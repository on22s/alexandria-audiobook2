"""Coordinated HTTP run state/health with mutable live state and native history."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script, voicelab as v
from tests.test_voicelab_health import _write_run, _empty_manifest, _adapter_with_recovery, _iso
from tests import test_voicelab_run_state_js as run_state


class VoicelabStatusSnapshotTests(unittest.TestCase):
    def test_http_health_uses_captured_state_and_eta_during_live_changes(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            history, models = root / 'history', root / 'models'
            history.mkdir(); models.mkdir()
            manifest = _empty_manifest(str(models))
            _write_run(str(history), 'run_active', 'running', _iso(-30),
                       stages=[{'name': 'train', 'started_at': _iso(-20)}])
            state = {'running': True, 'run_id': 'run_active', 'paused': False, 'current_task_idx': 0,
                     'tasks': [{'name': 'train', 'status': 'running'}], 'logs': ['original'],
                     'process': threading.Lock(), 'processes': [threading.Lock()]}
            entered, release = threading.Event(), threading.Event()
            original = v._build_voicelab_health
            captured = []
            def build(**kwargs):
                captured.append(kwargs['state'])
                entered.set()
                if not release.wait(3):
                    raise AssertionError('snapshot fixture release timed out')
                return original(**kwargs)
            for owner, name, value in ((script, 'process_state', {'voicelab': state}),
                                       (v, 'process_state', {'voicelab': state}),
                                       (v, 'RUN_HISTORY_DIR', str(history)),
                                       (v, 'LORA_MODELS_DIR', str(models)),
                                       (v, 'LORA_MODELS_MANIFEST', manifest)):
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(script, 'read_manual_pending', return_value=None))
            eta = {'eta_seconds': 12, 'progress': '1/4'}
            estimator = stack.enter_context(patch.object(script, '_compute_eta', return_value=eta))
            stack.enter_context(patch.object(v, '_compute_eta', side_effect=AssertionError('ETA recomputed')))
            stack.enter_context(patch.object(v, '_build_voicelab_health', side_effect=build))
            app = FastAPI(); app.include_router(script.router)
            client = stack.enter_context(TestClient(app))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.get, '/api/status/voicelab?include_health=true')
                try:
                    self.assertTrue(entered.wait(3))
                    state['running'] = False
                    state['paused'] = True
                    state['tasks'][0]['name'] = 'name'
                    state['logs'].append('later')
                finally:
                    release.set()
                response = future.result(timeout=3)
            self.assertEqual(200, response.status_code, response.text)
            report = response.json()
            self.assertTrue(report['running']); self.assertFalse(report['paused'])
            self.assertEqual(['original'], report['logs'])
            self.assertEqual('train', report['tasks'][0]['name'])
            self.assertTrue(report['health']['running'])
            self.assertEqual('train', report['health']['active_run']['stage'])
            self.assertEqual(12, report['health']['active_run']['eta_seconds'])
            self.assertEqual('1/4', report['health']['active_run']['progress'])
            estimator.assert_called_once()
            self.assertNotIn('process', report); self.assertNotIn('processes', report)
            self.assertIsNot(captured[0], state)
            self.assertIsNot(captured[0]['tasks'], state['tasks'])
            self.assertNotIn('eta', state); self.assertNotIn('health', state)
            self.assertEqual('name', state['tasks'][0]['name'])
            self.assertEqual(['original', 'later'], state['logs'])

    def test_native_history_recovery_and_legacy_status_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); history, models = root / 'history', root / 'models'
            history.mkdir(); models.mkdir()
            manifest = _adapter_with_recovery(str(models), 'alice')
            _write_run(str(history), 'run_success', 'completed', _iso(-30), finished_at=_iso(-20))
            state = {'running': False, 'tasks': [], 'logs': [], 'status': 'done'}
            before = copy.deepcopy(state)
            for owner, name, value in ((script, 'process_state', {'voicelab': state, 'other': state}),
                                       (v, 'RUN_HISTORY_DIR', str(history)),
                                       (v, 'LORA_MODELS_DIR', str(models)),
                                       (v, 'LORA_MODELS_MANIFEST', manifest)):
                stack.enter_context(patch.object(owner, name, value))
            app = FastAPI(); app.include_router(script.router)
            with TestClient(app) as client:
                bundled = client.get('/api/status/voicelab?include_health=true')
                self.assertEqual(200, bundled.status_code, bundled.text)
                data = bundled.json()
                self.assertEqual('done', data['status'])
                self.assertEqual('recovery_required', data['health']['status'])
                self.assertEqual('run_success', data['health']['last_success']['id'])
                self.assertEqual('alice', data['health']['pending_recovery'][0]['adapter_id'])
                self.assertNotIn('health', client.get('/api/status/voicelab').json())
                self.assertNotIn('health', client.get('/api/status/other?include_health=true').json())
                self.assertEqual(404, client.get('/api/status/missing?include_health=true').status_code)
            self.assertEqual(before, state)


class VoicelabStatusHealthJsTests(unittest.TestCase):
    run_js = run_state.VoicelabRunStateJsTests.run_js

    def test_old_standalone_success_cannot_replace_new_bundled_health(self):
        self.run_js(r"""
const old=deferred();ctx.API.get=()=>old.promise;
const refresh=ctx.refreshVoicelabHealth();await flush();
ctx.pollVoicelab('A');ctx.poll.options.onTick({running:false,tasks:[],health:{status:'ok'}});
old.resolve({status:'running'});await refresh;
assert(element('vl-health-body').innerHTML.includes('>ok<'));assert(!element('vl-health-body').innerHTML.includes('>running<'));
""")

    def test_old_standalone_error_cannot_erase_bundled_recovery(self):
        self.run_js(r"""
const old=deferred();ctx.API.get=()=>old.promise;
const refresh=ctx.refreshVoicelabHealth();await flush();
ctx.pollVoicelab('A');ctx.poll.options.onTick({running:false,tasks:[],health:{status:'recovery_required',pending_recovery:[{adapter_id:'alice'}]}});
old.reject(Error('old request failed'));await refresh;
assert(element('vl-health-body').innerHTML.includes('Recovery required: alice'));assert(!element('vl-health-body').innerHTML.includes('unavailable'));
""")
