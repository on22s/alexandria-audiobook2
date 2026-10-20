"""Two ASGI server processes share a real dataset generation task lease."""
import multiprocessing
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import test_dataset_builder_ownership as fixtures


def generate_in_foreign_server(root, ready, release, result):
    import core
    from routers import dataset_builder as routes
    root = Path(root)
    core.DATA_DIR = str(root)
    core._task_claims = {}
    states = {'dataset_builder': {'running': False, 'cancel': False, 'logs': []}}
    core.process_state = routes.process_state = states
    routes.DATASET_BUILDER_DIR = str(root / 'builder')
    # This callback only copies synthetic PCM; use the real CPU task lease.
    claim = core.claim_gpu_task
    routes.claim_gpu_task = lambda name, **kwargs: claim(name, cpu_only=True, **kwargs)
    def generate(**kwargs):
        preview = root / 'preview.wav'
        preview.write_bytes((root / 'builder/voice/sample_000.wav').read_bytes())
        ready.set()
        if not release.wait(15):
            raise AssertionError('foreign fixture was not released')
        return str(preview), 'fixture'
    routes.project_manager = SimpleNamespace(get_engine=lambda: SimpleNamespace(generate_voice_design=generate))
    api = FastAPI()
    api.include_router(routes.router)
    with TestClient(api) as client:
        response = client.post('/api/dataset_builder/generate_sample', json={
            'dataset_name': 'voice', 'sample_index': 0, 'description': 'Fixture',
            'text': 'First reference', 'seed': 17})
        result.put((response.status_code, response.text))


class DatasetBuilderForeignOwnershipTests(unittest.TestCase):
    def exercise(self, operation, payload, expected_status=409):
        import core
        ctx = multiprocessing.get_context('spawn')
        with fixtures.DatasetBuilderOwnershipTests().fixture() as (root, builder, work, output, states, api):
            states['dataset_builder']['running'] = False
            ready, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
            child = ctx.Process(target=generate_in_foreign_server,
                                args=(str(root), ready, release, result))
            child.start()
            try:
                self.assertTrue(ready.wait(8), 'foreign server never entered synthetic generation')
                before = {p: p.read_bytes() for p in work.iterdir() if p.is_file()}
                if operation == 'edit_rows':
                    from routers.dataset_builder import get_dataset_row_revision
                    row = json.loads((work / 'state.json').read_bytes())['samples'][0]
                    payload = {'name': 'voice', 'expected_count': 2, 'edits': [
                        {'index': 0, 'expected_revision': get_dataset_row_revision(row),
                         'row': {'text': 'Changed'}}]}
                with patch.object(core, 'DATA_DIR', str(root)), TestClient(api) as client:
                    response = (client.delete('/api/dataset_builder/voice') if operation == 'delete'
                                else client.post('/api/dataset_builder/' + operation, json=payload))
                self.assertEqual(expected_status, response.status_code, response.text)
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                self.assertFalse((output / 'voice').exists())
            finally:
                release.set()
                child.join(8)
                if child.is_alive():
                    child.kill()
                    child.join(3)
                outcome = result.get(timeout=2) if child.exitcode == 0 else None
                result.close()
            self.assertEqual(0, child.exitcode)
            self.assertEqual(200, outcome[0], outcome)

    def test_mutations_refuse_foreign_generation_and_preserve_project_bytes(self):
        for operation, payload in (
                ('delete', None),
                ('update_meta', {'name': 'voice', 'description': 'Changed'}),
                ('update_rows', {'name': 'voice', 'rows': []}),
                ('edit_rows', None),
                ('save', {'name': 'voice', 'ref_index': 0})):
            with self.subTest(operation=operation):
                self.exercise(operation, payload)

    def test_unrelated_project_metadata_stays_editable_during_foreign_generation(self):
        self.exercise('update_meta', {'name': 'other', 'description': 'Independent'}, expected_status=200)
