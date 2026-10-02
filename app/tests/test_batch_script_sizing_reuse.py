import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routers import script
from three_pass_generate import build_three_pass_request_preflight


class BatchScriptSizingReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'book.txt'
        self.text = 'Alice entered. "Hello," she said. Bob walked away.'
        self.path.write_text(self.text)
        self.config = {'generation': {}, 'prompts': {}}
        for context in (patch.object(script, 'UPLOADS_DIR', self.tmp.name),
                        patch.object(script, 'load_app_config', return_value=self.config),
                        patch.object(script, 'get_planned_ideal_settings', return_value={
                            'context_length': 100000, 'parallel': 2})):
            context.start()
            self.addCleanup(context.stop)
        self.request = script.BatchScriptRequest(tasks=[{'filename': 'book.txt'}])

    def test_preparation_and_dispatch_scan_once_without_mutating_input(self):
        original = self.request.model_dump()
        with patch.object(script, 'build_three_pass_request_preflight',
                          wraps=build_three_pass_request_preflight) as scan:
            jobs = script.get_prepared_batch_script_jobs(self.request)
            before = copy.deepcopy(jobs)
            cached = script.build_batch_script_preflight(jobs)
            self.assertEqual(scan.call_count, 1)
            self.assertIsNone(script.three_pass_refusal(jobs))
            self.assertEqual(scan.call_count, 1)
            uncached = script.build_batch_script_preflight([
                {key: value for key, value in jobs[0].items() if key != 'prepared_source'}])
            self.assertEqual(cached, uncached)
            self.assertEqual(jobs, before)
            self.assertEqual(self.request.model_dump(), original)
            self.assertNotIn('requests', jobs[0]['prepared_source']['preflight']['report'])

    def test_live_capacity_reduces_workers_without_rescanning(self):
        jobs = script.get_prepared_batch_script_jobs(self.request)
        jobs = [jobs[0], {**jobs[0], 'filename': 'second.txt'}]
        worst = jobs[0]['prepared_source']['preflight']['report']['worst_predicted_tokens']
        with patch.object(script, 'build_three_pass_request_preflight',
                          side_effect=AssertionError('unexpected rescan')):
            with patch.object(script, 'get_planned_ideal_settings', return_value={
                    'context_length': worst * 2 + 1, 'parallel': 2}):
                self.assertEqual(script.build_batch_script_preflight(jobs)['workers'], 2)
            with patch.object(script, 'get_planned_ideal_settings', return_value={
                    'context_length': worst + 1, 'parallel': 2}):
                self.assertEqual(script.build_batch_script_preflight(jobs)['workers'], 1)

    def test_source_settings_windows_and_prompts_invalidate(self):
        for changed in ('source', 'settings', 'windows', 'prompts'):
            with self.subTest(changed=changed):
                self.path.write_text(self.text)
                self.config['generation'] = {}
                jobs = script.get_prepared_batch_script_jobs(self.request)
                if changed == 'source':
                    self.path.write_text(self.text + ' A new ending.')
                elif changed == 'settings':
                    self.config['generation']['three_pass_chunk_size'] = 1500
                elif changed == 'windows':
                    self.config['generation']['context_rescue_windows'] = [100, 200]
                context = (patch.object(script, 'load_instruct_prompts',
                                         return_value=('changed', '{batch}'))
                           if changed == 'prompts' else patch.object(
                               script, 'load_instruct_prompts', wraps=script.load_instruct_prompts))
                with context, patch.object(script, 'build_three_pass_request_preflight',
                                           wraps=build_three_pass_request_preflight) as scan:
                    script.build_batch_script_preflight(jobs)
                    self.assertEqual(scan.call_count, 1)

    def test_refusal_matches_uncached_after_settings_change(self):
        jobs = script.get_prepared_batch_script_jobs(self.request)
        self.config['generation']['three_pass_segmentation'] = 'quotes'
        self.path.write_text('No quotation marks in this source. ' * 120)
        uncached = [{key: value for key, value in jobs[0].items() if key != 'prepared_source'}]
        self.assertEqual(script.three_pass_refusal(jobs), script.three_pass_refusal(uncached))
        self.assertIn('does not seem to mark', script.three_pass_refusal(jobs))
