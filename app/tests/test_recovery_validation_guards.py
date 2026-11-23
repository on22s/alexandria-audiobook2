"""Admission-gap races and real process-death boundaries for #2207.

Companion to ``test_recovery_validation_matrix``: it checks that changes in
the window between admission and publication, and an actual writer process
killed at a durable boundary, leave the receipt/checkpoint/manifest bytes and
fresh-process recovery intact. Every case is recorded in the shared matrix
artifact (``AA2_RECOVERY_MATRIX_ARTIFACT``).
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routers import script
from three_pass_generate import three_pass_checkpoint_path, three_pass_manifest_path
from utils import atomic_json_write

from tests.test_recovery_validation_matrix import (
    SOURCE, _ARTIFACT, _Harness, _config, _digest, _factory, _payloads, _write_artifact)


class AdmissionGapRaces(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _write_artifact()

    def _case(self, name):
        return {'case': name, 'inputs': {}, 'order': [], 'expected': {},
                'observed': {}, 'artifacts': {}, 'comparison': {}}

    def _protected(self, harness):
        return {name: _digest(path) for name, path in harness.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def _record(self, case, harness, receipt_path, *, expected, observed):
        case['expected'] = expected
        case['observed'] = observed
        case['artifacts'] = harness.snapshot()
        case['artifacts']['receipt_path'] = _digest(receipt_path)
        _ARTIFACT['cases'].append(copy.deepcopy(
            {key: value for key, value in case.items() if not key.startswith('_')}))

    @staticmethod
    def _write_state(path, updates):
        state = json.loads(Path(path).read_text())
        state.update(updates)
        atomic_json_write(state, path)

    @staticmethod
    def _write_config(harness, model):
        config = json.loads((harness.root / 'config.json').read_text())
        config['llm']['model_name'] = model
        atomic_json_write(config, harness.root / 'config.json')

    def _gap_case(self, name, mutate, *, cast=False, expected_status=409,
                  entries=None):
        case = self._case(name)
        harness = _Harness(case, cast=cast)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        harness.worker(command, _factory(_payloads(), fail=('attribute',)))
        state_path = harness.root / 'state.json'
        state = json.loads(state_path.read_text())
        snapshot = {'receipt_sha256': receipt['sha256'],
                    'book_generation': state.get('book_generation'),
                    'source_path': state.get('input_file_path'),
                    'selection': state.get('script_generation_selection'),
                    'options': state.get('script_generation_options'),
                    'config_sha256': hashlib.sha256(
                        (harness.root / 'config.json').read_bytes()).hexdigest()}
        selection = state.get('script_generation_selection')
        before = self._protected(harness)
        mutate(harness, state_path)
        if entries is None:
            entries = ([{'n': 0, 'speaker': 'MIREILLE'}] if cast else [{'n': 0, 'speaker': 'MIRA'}])
        try:
            result = script.apply_manual_recovery(entries, 'manual',
                                                  admission_selection=selection,
                                                  admission_snapshot=snapshot)
            status = 200 if result.get('accepted') else 422
        except HTTPException as error:
            status = error.status_code
        after = self._protected(harness)
        self.assertEqual(expected_status, status)
        self.assertEqual(before, after)
        case['order'].append('admission-gap mutation -> apply -> %s' % status)
        self._record(case, harness, receipt_path,
                     expected={'status': expected_status, 'artifacts_preserved': True},
                     observed={'status': status, 'artifacts_preserved': before == after})

    def test_admission_gap_races_preserve_all_artifacts(self):
        races = {
            'book-generation': lambda h, p: self._write_state(p, {'book_generation': 'raced'}),
            'selection': lambda h, p: self._write_state(p, {'script_generation_selection': {
                'book_token': 'x' * 64, 'source_path': str(h.source),
                'source_sha256': hashlib.sha256(h.source.read_bytes()).hexdigest()}}),
            'source-path': lambda h, p: self._write_state(
                p, {'input_file_path': str(h.root / 'other.txt')}),
            'source-bytes': lambda h, p: h.source.write_text('Changed source bytes.', encoding='utf-8'),
            'settings': lambda h, p: self._write_state(p, {'script_generation_options': {
                'first_person_narrator': 'RACED'}}),
            'profile': lambda h, p: self._write_config(h, 'live-changed'),
        }
        for name, mutate in races.items():
            with self.subTest(race=name):
                self._gap_case('race/admission-gap/' + name, mutate)

    def test_task_ownership_race_preserves_all_artifacts(self):
        case = self._case('race/admission-gap/task-ownership')
        harness = _Harness(case)
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        harness.worker(command, _factory(_payloads(), fail=('attribute',)))
        before = self._protected(harness)
        harness.states['script']['running'] = True
        app = FastAPI()
        app.include_router(script.router)
        with TestClient(app) as client:
            response = client.post('/api/generate_script/inject',
                                   json={'entries': [{'n': 0, 'speaker': 'MIRA'}]})
        harness.states['script']['running'] = False
        after = self._protected(harness)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, after)
        case['order'].append('task-running -> inject -> %s' % response.status_code)
        self._record(case, harness, receipt_path,
                     expected={'status': 409, 'artifacts_preserved': True},
                     observed={'status': response.status_code,
                               'artifacts_preserved': before == after})

    def test_cast_replaced_after_admission_cannot_exempt_an_alias(self):
        def replace_cast(harness, state_path):
            digest = hashlib.sha256(harness.source.read_bytes()).hexdigest()
            (harness.root / 'script_run_cast.json').write_bytes(json.dumps(
                {'cast': [{'name': 'MIRA', 'aliases': ['RACED']}]}).encode())
        self._gap_case('race/admission-gap/cast', replace_cast, cast=True,
                       expected_status=409, entries=[{'n': 0, 'speaker': 'MIREILLE'}])


CHILD_ADMISSION = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
root, boundary, appdir = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, appdir)
from routers import script
import core
script.DATA_DIR = str(root)
script.SCRIPT_PATH = str(root / 'annotated_script.json')
script.CONFIG_PATH = str(root / 'config.json')
states = {'script': {'running': False, 'logs': [], 'cancel': False}}
core.process_state = states
script.process_state = states
core._task_claims = {}
core._gpu_leases = {}
original = script.apply_book_state_locked
def publisher(*args, **kwargs):
    if boundary == 'admission-before':
        os._exit(91)
    result = original(*args, **kwargs)
    if boundary == 'admission-after':
        os._exit(92)
    return result
patches = [
    patch.object(script, 'apply_book_state_locked', side_effect=publisher),
    patch.object(script, 'check_global_gpu_lock'),
    patch.object(script, 'get_active_reasoning_effort', return_value=None),
    patch.object(script, 'reserve_background_task', return_value='a' * 32),
    patch.object(script, 'register_claimed_background_task', lambda *a, **kw: None),
    patch.object(core, 'process_state', states),
    patch.object(script, 'process_state', states),
    patch.object(core, '_task_claims', {'script': {'id': 'a' * 32, 'phase': 'pending'}}),
    patch.object(core, '_gpu_leases', {}),
    patch.object(core, 'acquire_gpu_lock', return_value=None),
]
for item in patches:
    item.start()
script.start_script_generation(script.BackgroundTasks(), str(root / 'book.txt'), None)
raise AssertionError('publication boundary was never reached')
'''

CHILD_RECOVERY = r'''
import hashlib, json, os, sys
from pathlib import Path
from unittest.mock import patch
root, boundary, appdir, entries = Path(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path.insert(0, appdir)
from routers import script
import core
script.DATA_DIR = str(root)
script.SCRIPT_PATH = str(root / 'annotated_script.json')
script.CONFIG_PATH = str(root / 'config.json')
states = {'script': {'running': False, 'logs': [], 'cancel': False}}
core.process_state = states
script.process_state = states
receipt = script.run_spec_receipt.load_receipt(str(root))
state = json.loads((root / 'state.json').read_text())
snapshot = {'receipt_sha256': receipt['sha256'],
            'book_generation': state.get('book_generation'),
            'source_path': state.get('input_file_path'),
            'selection': state.get('script_generation_selection'),
            'options': state.get('script_generation_options'),
            'config_sha256': hashlib.sha256((root / 'config.json').read_bytes()).hexdigest()}
original = script.apply_book_state_locked
def publisher(*args, **kwargs):
    if boundary == 'recovery-before':
        os._exit(93)
    result = original(*args, **kwargs)
    if boundary == 'recovery-after':
        os._exit(94)
    return result
with patch.object(script, 'apply_book_state_locked', side_effect=publisher):
    script.apply_manual_recovery(entries, 'manual',
                                 admission_selection=state.get('script_generation_selection'),
                                 admission_snapshot=snapshot)
'''


class ProcessDeathMatrix(unittest.TestCase):
    """Kill a real writer at each durable publication boundary and restart."""

    @classmethod
    def tearDownClass(cls):
        _write_artifact()

    def _child(self, code, root, boundary, entries=None):
        appdir = str(Path(__file__).resolve().parent.parent)
        argv = [sys.executable, '-c', code, str(root), boundary, appdir]
        if entries is not None:
            argv.append(json.dumps(entries))
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                              cwd=appdir, env=env)

    def _plain_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'book.txt').write_text(SOURCE, encoding='utf-8')
        atomic_json_write({'input_file_path': str(root / 'book.txt'),
                           'script_generation_input_file': str(root / 'book.txt')},
                          root / 'state.json')
        atomic_json_write(_config(), root / 'config.json')
        return root

    def _case(self, name, root, *, expected, observed):
        case = {'case': name,
                'inputs': {'root_files': sorted(path.name for path in root.iterdir())},
                'order': observed.get('order', []), 'expected': expected,
                'observed': observed, 'artifacts': {}, 'comparison': {}}
        for label, path in (
                ('receipt', script.run_spec_receipt.receipt_path(str(root))),
                ('state', str(root / 'state.json')),
                ('checkpoint', three_pass_checkpoint_path(str(root / 'annotated_script.json'))),
                ('manifest', three_pass_manifest_path(str(root / 'annotated_script.json')))):
            case['artifacts'][label] = _digest(path)
        _ARTIFACT['cases'].append(copy.deepcopy(
            {key: value for key, value in case.items() if not key.startswith('_')}))

    def test_real_process_death_at_receipt_and_state_publication(self):
        for boundary, code in (('admission-before', 91), ('admission-after', 92)):
            with self.subTest(boundary=boundary):
                root = self._plain_fixture()
                state_before = (root / 'state.json').read_bytes()
                receipt_path = Path(script.run_spec_receipt.receipt_path(str(root)))
                result = self._child(CHILD_ADMISSION, root, boundary)
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                published = receipt_path.exists()
                self.assertEqual(boundary == 'admission-after', published)
                if boundary == 'admission-before':
                    self.assertEqual(state_before, (root / 'state.json').read_bytes())
                else:
                    fresh = script.run_spec_receipt.load_receipt(str(root))
                    self.assertTrue(fresh['spec']['params'])
                    self.assertNotEqual(state_before, (root / 'state.json').read_bytes())
                self._case('process-death/' + boundary, root,
                           expected={'receipt_published': boundary == 'admission-after'},
                           observed={'exit_code': result.returncode,
                                     'receipt_published': published,
                                     'order': ['child-admission', boundary, 'fresh-read']})

    def _failed_fixture(self):
        harness = _Harness({'case': 'process-death/recovery-fixture'})
        self.addCleanup(harness.close)
        command, receipt_path, digest, receipt = harness.admit()
        harness.worker(command, _factory(_payloads(), fail=('attribute',)))
        return harness, receipt_path

    def test_real_process_death_at_checkpoint_and_manifest_publication(self):
        for boundary, code in (('recovery-before', 93), ('recovery-after', 94)):
            with self.subTest(boundary=boundary):
                harness, receipt_path = self._failed_fixture()
                checkpoint = Path(three_pass_checkpoint_path(str(harness.script_path)))
                manifest = Path(three_pass_manifest_path(str(harness.script_path)))
                before = (checkpoint.read_bytes(), manifest.read_bytes(),
                          Path(receipt_path).read_bytes())
                result = self._child(CHILD_RECOVERY, harness.root, boundary,
                                     entries=[{'n': 0, 'speaker': 'MIRA'}])
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                after = (checkpoint.read_bytes(), manifest.read_bytes(),
                         Path(receipt_path).read_bytes())
                self.assertEqual(before[2], after[2])
                if boundary == 'recovery-before':
                    self.assertEqual(before, after)
                else:
                    self.assertNotEqual(before[0], after[0])
                    app = FastAPI()
                    app.include_router(script.router)
                    with patch.object(script, 'SCRIPT_PATH', str(harness.script_path)), \
                            patch.object(script, 'DATA_DIR', str(harness.root)), \
                            patch.object(script, 'CONFIG_PATH',
                                         str(harness.root / 'config.json')), \
                            patch.object(script, 'process_state', harness.states):
                        with TestClient(app) as client:
                            detail = client.get('/api/generate_script/recovery/detail')
                    self.assertEqual(200, detail.status_code)
                    self.assertFalse(detail.json().get('recoverable'))
                self._case('process-death/' + boundary, harness.root,
                           expected={'checkpoint_changed': boundary == 'recovery-after'},
                           observed={'exit_code': result.returncode,
                                     'checkpoint_changed': before[0] != after[0],
                                     'order': ['child-recovery', boundary, 'fresh-read']})


if __name__ == '__main__':
    unittest.main()
