"""Actual stage dispatch with native report files and synthetic worker results."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_benchmark_cancel_boundaries import runner
import benchmark_core as core

STAGES = [('tts_generation', 'run_tts_generation_benchmark', '_validate_tts_fixture'),
          ('persona_generation', 'run_persona_generation_benchmark', '_validate_persona_fixture'),
          ('nickname_detection', 'run_nickname_detection_benchmark', '_validate_nickname_fixture'),
          ('script_generation', 'run_script_generation_benchmark', None),
          ('script_review', 'run_script_review_benchmark', None)]


class BenchmarkBatchLifecycleTests(unittest.TestCase):
    def execute(self, stage, run_name, validate_name, *, cancel=False, crash=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
            manifest = {'schema_version': 1, 'stage': stage,
                        'targets': ['local' if stage == 'tts_generation' else 'thunder'],
                        'repetitions': 2, 'fixtures': [{'id': 'first', 'sha256': 'a'}]}
            original = copy.deepcopy(manifest)
            environment = core.build_environment_fingerprint(manifest['targets'][0], {
                'hostname': 'fixture', 'gpu_name': 'none', 'backend': 'cpu',
                'python_version': 'fixture', 'git_commit': 'fixture'})
            path = str(Path(tmp) / 'report.json')
            state = {'cancel': False, 'status': 'running', 'logs': [],
                     'tasks': [{'fixture_id': 'first', 'status': 'pending'}]}
            if validate_name:
                patches.enter_context(patch.object(runner, validate_name))
            patches.enter_context(patch.object(runner, 'load_app_config', return_value={}))
            patches.enter_context(patch.object(runner, '_get_llm_benchmark_target',
                return_value=({'model_name': 'fixture'}, {'context_length': 4096})))
            patches.enter_context(patch.object(runner, 'make_llm_client', return_value=object()))
            patches.enter_context(patch.object(runner, '_measure_llm_network_rtt', return_value=0))
            patches.enter_context(patch.object(runner, 'get_text_fixture_sources',
                return_value={'first': 'one two three'}))
            patches.enter_context(patch.object(runner, '_load_review_fixture',
                return_value=[{'speaker': 'NARRATOR', 'text': 'one two three'}]))
            metrics = {'duration_seconds': 1, 'silence_ratio': 0, 'clipping_ratio': 0}
            first = {'fixture_id': 'first', 'repetition': 1, 'status': 'passed', 'metrics': metrics}
            before_case = copy.deepcopy(first)
            def results(*args):
                payload = args[0] if stage == 'tts_generation' else args[1]
                self.assertEqual([1, 2], payload['fixtures'][0]['repetition_numbers'])
                state['cancel'] = cancel
                if crash:
                    def interrupted():
                        yield first
                        raise RuntimeError('worker interrupted after first result')
                    return interrupted()
                return [first]
            name = '_run_tts_worker' if stage == 'tts_generation' else '_run_llm_worker'
            worker = patches.enter_context(patch.object(runner, name, side_effect=results))
            if cancel:
                report = getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
                self.assertEqual('cancelled', state['status'])
            else:
                with self.assertRaisesRegex(RuntimeError, 'worker interrupted' if crash else 'missing benchmark cases'):
                    getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
                self.assertNotEqual('complete', state['status'])
            worker.assert_called_once()
            saved = json.loads(Path(path).read_text())
            self.assertEqual(1, len(saved['cases']))
            self.assertEqual(first, before_case)
            state.update(cancel=False, status='running')
            def remaining(*args):
                payload = args[0] if stage == 'tts_generation' else args[1]
                self.assertEqual([2], payload['fixtures'][0]['repetition_numbers'])
                # Failed quality is still an attempted result, not a missing case.
                return [{'fixture_id': 'first', 'repetition': 2, 'status': 'failed'}]
            worker.reset_mock(); worker.side_effect = remaining
            resumed = getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
            worker.assert_called_once()
            self.assertEqual(saved['cases'], resumed['cases'][:1])
            self.assertEqual('failed', resumed['cases'][1]['status'])
            self.assertEqual(resumed, json.loads(Path(path).read_text()))
            self.assertEqual('complete', state['status'])
            self.assertEqual('done', state['tasks'][0]['status'])
            self.assertEqual(original, manifest)
            # A fully saved batch cannot overwrite a newly queued cancellation.
            state.update(cancel=True, status='running')
            worker.reset_mock()
            cached = getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
            worker.assert_not_called()
            self.assertEqual(resumed['cases'], cached['cases'])
            self.assertEqual('cancelled', state['status'])

    def test_missing_worker_results_preserve_prefix_and_resume_only_missing(self):
        for case in STAGES:
            with self.subTest(stage=case[0]):
                self.execute(*case)

    def test_cancelled_partial_batch_preserves_prefix_and_resumes(self):
        for case in STAGES:
            with self.subTest(stage=case[0]):
                self.execute(*case, cancel=True)

    def test_worker_interruption_preserves_prefix_and_resumes(self):
        for case in STAGES:
            with self.subTest(stage=case[0]):
                self.execute(*case, crash=True)
