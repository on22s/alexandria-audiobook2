"""Every public lifecycle view agrees while a failed owner keeps admission."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import core
from routers import benchmark, dataset_builder, script, system


class RetainedTaskStatusTests(unittest.TestCase):
    def test_discovery_detail_benchmark_dataset_and_eta_agree_on_failed_ownership(self):
        diagnostic = 'Win32 error 5; Job member PIDs: 9001, 9002. End them in Task Manager or restart the app.'
        process = Mock()
        process._alexandria_windows_owner.job.handle = 123
        states = {name: dict(running=False, status='failed', _windows_cleanup_failed=True,
                            processes=[process], logs=[diagnostic], dataset_name='fixture')
                  for name in ('dataset_builder', 'benchmark', 'script')}
        app = FastAPI()
        for router in (script.router, benchmark.router, dataset_builder.router):
            app.include_router(router)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'fixture').mkdir()
            (root / 'fixture' / 'state.json').write_text('{}')
            with patch.object(core, 'process_state', states), \
                 patch.object(core, '_task_claims', {'script': {'id': 'owned-claim', 'phase': 'cleanup_failed'}}), \
                 patch.object(script, 'process_state', states), \
                 patch.object(benchmark, 'process_state', states), \
                 patch.object(dataset_builder, 'process_state', states), \
                 patch.object(system, 'process_state', states), \
                 patch.object(dataset_builder, 'DATASET_BUILDER_DIR', str(root)), \
                 TestClient(app) as client:
                discovery = client.get('/api/status').json()
                detail = client.get('/api/status/dataset_builder').json()
                activity = client.get('/api/dataset_builder/status/fixture').json()
                benchmark_status = client.get('/api/benchmark/status').json()
                eta = asyncio.run(system.get_eta_status())
                delivery = asyncio.run(script.get_delivery_retry_status('owned-claim'))
                for view in (discovery['dataset_builder'], detail, activity,
                             discovery['benchmark'], benchmark_status, eta, delivery):
                    self.assertFalse(view['running'], view)
                    self.assertEqual('failed', view['status'])
                    self.assertTrue(view['ownership_unconfirmed'])
                self.assertFalse(activity['global_running'])
                self.assertTrue(activity['global_ownership_unconfirmed'])
                self.assertTrue(activity['busy'])
                self.assertEqual([diagnostic], activity['logs'])
                self.assertEqual([diagnostic], eta['logs'])
                self.assertEqual([diagnostic], delivery['logs'])
                other_delivery = asyncio.run(script.get_delivery_retry_status('other-claim'))
                self.assertFalse(other_delivery['ownership_unconfirmed'])
                self.assertEqual([], other_delivery['logs'])
                other_activity = dataset_builder.get_builder_activity('other')
                self.assertFalse(other_activity['running'])
                self.assertFalse(other_activity['ownership_unconfirmed'])
                self.assertTrue(other_activity['global_busy'])
                self.assertTrue(other_activity['global_ownership_unconfirmed'])
                self.assertEqual([], other_activity['logs'])
                self.assertTrue(core.is_task_running('dataset_builder'))
                with self.assertRaises(HTTPException) as raised:
                    dataset_builder._require_builder_idle('fixture', 'Retained owner')
                self.assertEqual(409, raised.exception.status_code)
