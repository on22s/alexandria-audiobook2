"""Structured book-sample results must retain the native pipeline evidence."""
import json
from pathlib import Path
import tempfile
import unittest
import three_pass_generate as tp
from tests import test_three_pass_cli_settings

class BookPreflightTests(unittest.TestCase):
    def test_cli_records_full_and_sample_plans(self):
        config = {'llm_mode': 'local', 'llm_local': {'model_name': 'fixture'},
                  'generation': {'max_tokens': 4096}}
        with tempfile.TemporaryDirectory() as tmp:
            calls, plans = test_three_pass_cli_settings.ThreePassCliSettingsTests().run_cli(tmp, config, True, [])
            summary = json.loads(Path(tmp, 'sample.json.preflight_manifest.json').read_text())
        self.assertIn('planned_calls', summary)
        self.assertEqual({str(k): v for k, v in calls[0][3]['planned_calls'].items()},
                         summary['samples'][0].get('planned_calls'))
        self.assertIn('failure_codes', summary['samples'][0])

    def test_incomplete_sample_is_not_reported_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'sample.json'))
            tp.atomic_json_write({'status': 'incomplete', 'progress': {
                'failure_codes': {'context_required': 2}}, 'failed_pass': 2},
                tp.three_pass_manifest_path(output))
            info = tp.get_preflight_sample_info('first', 0, output, {1: 1, 2: 2, 3: 1},
                                                entries=[{'text': 'accepted prefix'}])
        self.assertEqual('failed', info['status'])
        self.assertEqual({'context_required': 2}, info['failure_codes'])
        self.assertEqual(2, info['failed_pass'])
        self.assertEqual(1, info['entries'])

    def test_exhaustion_keeps_native_failure_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'sample.json'))
            tp.atomic_json_write({'status': 'failed', 'progress': {
                'failure_codes': {'coverage': 3}}, 'failed_chunk': 1},
                tp.three_pass_manifest_path(output))
            info = tp.get_preflight_sample_info('middle', 4, output, {1: 1},
                                                error='sample exhausted')
        self.assertEqual('failed', info['status'])
        self.assertEqual({'coverage': 3}, info['failure_codes'])
        self.assertEqual('sample exhausted', info['error'])
        self.assertEqual(4, info['chunk_index'])
