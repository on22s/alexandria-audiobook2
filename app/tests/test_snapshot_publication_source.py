import asyncio
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import core
from publication_source import get_publication_source_capture, get_publication_source_path
from routers import script
from tests import test_saved_publication_source as fixtures
from tests import test_saved_book_publication as saved


class SnapshotPublicationSourceTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with fixtures.SavedPublicationSourceTests().fixture() as (root, _), ExitStack() as stack:
            for key, value in (('DATA_DIR', root), ('SCRIPTS_DIR', root / 'scripts'),
                    ('SCRIPT_PATH', root / 'annotated_script.json'), ('VOICE_CONFIG_PATH', root / 'voice_config.json')):
                stack.enter_context(patch.object(script, key, str(value)))
            states = {'script': {'running': True, 'logs': ['generation still running']}}
            stack.enter_context(patch.object(script, 'process_state', states))
            entry = {'speaker': 'NARRATOR', 'text': 'Shareable source paragraph.', 'instruct': 'Neutral.'}
            checkpoint = {'stage': 'instruct', 'chunks_done': 1, 'segmented': [entry, entry],
                          'named': [entry, entry], 'annotated': [entry, None]}
            Path(script.three_pass_checkpoint_path(script.SCRIPT_PATH)).write_text(json.dumps(checkpoint))
            api = FastAPI(); api.include_router(script.router)
            @api.get('/ping')
            async def ping():
                return {'ok': True}
            yield root, api, states

    def artifacts(self, root):
        return saved.SavedBookPublicationTests().artifacts(root)

    def test_finished_prefix_and_provenance_commit_together_without_changing_the_run(self):
        with self.fixture() as (root, api, states), TestClient(api) as client:
            before = json.dumps(states, sort_keys=True)
            checkpoint = Path(script.three_pass_checkpoint_path(script.SCRIPT_PATH)).read_bytes()
            response = client.post('/api/generate_script/snapshot', json={'name': 'snapshot'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(1, response.json()['entries'])
            path = root / 'scripts/snapshot.json'
            self.assertEqual(1, len(json.loads(path.read_bytes())))
            captured = get_publication_source_capture(path)
            self.assertEqual('current', captured['binding_status'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), captured['document']['binding']['sha256'])
            self.assertEqual('origin.epub', captured['document']['candidates'][0]['origin']['filename'])
            self.assertEqual(before, json.dumps(states, sort_keys=True))
            self.assertEqual(checkpoint, Path(script.three_pass_checkpoint_path(script.SCRIPT_PATH)).read_bytes())

    def test_provenance_publication_failure_restores_library_without_partial_snapshot(self):
        with self.fixture() as (root, api, states), TestClient(api, raise_server_exceptions=False) as client:
            before = self.artifacts(root); replace = os.replace; failed = False
            def refuse(source, destination, *args, **kwargs):
                nonlocal failed
                if not failed and str(destination) == get_publication_source_path(root / 'scripts/snapshot.json'):
                    failed = True
                    raise OSError('fixture snapshot provenance failure')
                return replace(source, destination, *args, **kwargs)
            with patch('os.replace', side_effect=refuse):
                self.assertEqual(500, client.post('/api/generate_script/snapshot', json={'name': 'snapshot'}).status_code)
            self.assertTrue(failed)
            self.assertEqual(before, self.artifacts(root))
            self.assertTrue(states['script']['running'])

    def test_orphan_companion_collision_refuses_without_overwriting_it(self):
        with self.fixture() as (root, api, _), TestClient(api) as client:
            Path(get_publication_source_path(root / 'scripts/snapshot.json')).write_bytes(b'prior companion')
            before = self.artifacts(root)
            self.assertEqual(409, client.post('/api/generate_script/snapshot', json={'name': 'snapshot'}).status_code)
            self.assertEqual(before, self.artifacts(root))

    def test_corrupt_source_provenance_or_voices_refuse_before_library_changes(self):
        for name in ('source', 'nested_source', 'voices'):
            with self.subTest(name=name), self.fixture() as (root, api, _), TestClient(api) as client:
                target = (Path(get_publication_source_path(root / 'uploads/source.txt'))
                          if name != 'voices' else root / 'voice_config.json')
                target.write_bytes(b'[' * 2000 + b']' * 2000 if name == 'nested_source' else b'not JSON')
                before = self.artifacts(root)
                self.assertEqual(409, client.post('/api/generate_script/snapshot', json={'name': 'snapshot'}).status_code)
                self.assertEqual(before, self.artifacts(root))

    def test_batch_review_destination_ownership_is_preserved(self):
        with self.fixture() as (root, api, _), TestClient(api) as client:
            before = self.artifacts(root)
            with patch.object(core, 'process_state', {'batch_review': {
                    'running': True, 'tasks': [{'name': 'snapshot'}]}}):
                self.assertEqual(409, client.post('/api/generate_script/snapshot', json={'name': 'snapshot'}).status_code)
            self.assertEqual(before, self.artifacts(root))

    def test_provenance_read_does_not_block_same_loop_http(self):
        with self.fixture() as (_, api, _):
            entered, release = threading.Event(), threading.Event()
            original = script.get_saved_publication_source_bytes; threads = []
            def paused(*args, **kwargs):
                threads.append(threading.get_ident()); entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture snapshot reader not released')
                return original(*args, **kwargs)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(api), base_url='http://fixture') as client:
                    task = asyncio.create_task(client.post('/api/generate_script/snapshot', json={'name': 'snapshot'}))
                    watchdog = threading.Timer(.5, release.set); watchdog.start()
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        self.assertEqual({'ok': True}, (await client.get('/ping')).json())
                        self.assertFalse(release.is_set()); self.assertFalse(task.done())
                        self.assertTrue(all(value != threading.get_ident() for value in threads))
                    finally:
                        release.set(); watchdog.cancel(); response = await task
                    self.assertEqual(200, response.status_code, response.text)
            with patch.object(script, 'get_saved_publication_source_bytes', side_effect=paused):
                asyncio.run(run())
