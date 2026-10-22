"""Read actual diagnostics routes without constructing or inspecting weights."""
import tempfile
import sys
import tts
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from project import ProjectManager
from routers import system
from synthesis_recovery_native import NativeRecoveryLoadedModel, get_native_recovery_load_observation


class ResidencyDiagnosticsTests(unittest.TestCase):
    def test_actual_http_report_keeps_uninitialized_engine_unloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            app = FastAPI()
            app.include_router(system.router)
            with patch.object(system, 'project_manager', manager), \
                 patch.object(manager, 'get_engine', side_effect=AssertionError('no load')), \
                 patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None), \
                 patch.dict(sys.modules, {'torch': None}), \
                 patch.object(system, 'get_runtime_info', return_value={'packages': {'torch': None}}), \
                 TestClient(app) as client:
                response = client.get('/api/tts/diagnostics')
            self.assertEqual(200, response.status_code)
            report = response.json()['sections']['tts']
            self.assertEqual('not_initialized', report['residency']['state'])
            self.assertEqual([], report['residency']['residents'])
            self.assertIsNone(report['runtime']['packages']['torch'])
            self.assertEqual(3, len(report['assets']))
            self.assertEqual({'not_cached'}, {row['state'] for row in report['assets']})
            self.assertEqual('runtime_not_loaded', report['device']['state'])
            self.assertIsNone(manager.engine)

    def test_busy_construction_is_reported_without_wait_or_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            with manager._engine_lock, patch.object(system, 'project_manager', manager), \
                 patch.object(system, 'get_runtime_info', return_value={}):
                result = system.get_tts_diagnostics()['sections']['tts']['residency']
            self.assertEqual({'state': 'busy', 'active_leases': None, 'residents': None}, result)
            self.assertIsNone(manager.engine)

    def test_load_capture_is_historical_and_absent_capture_stays_unknown(self):
        model = SimpleNamespace()
        self.assertIsNone(get_native_recovery_load_observation(model))
        model._aa2_recovery_loaded_model = NativeRecoveryLoadedModel('a'*64, 'b'*64, 'c'*64)
        result = get_native_recovery_load_observation(model)
        self.assertEqual('captured_at_load_not_revalidated', result['scope'])
        self.assertEqual('a'*64, result['asset_sha256'])
        result['asset_sha256'] = 'changed copy'
        self.assertEqual('a'*64, model._aa2_recovery_loaded_model.asset_sha256)
        model._aa2_recovery_loaded_model = {'asset_sha256': 'a'*64}
        self.assertIsNone(get_native_recovery_load_observation(model))
