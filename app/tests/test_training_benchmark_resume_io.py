"""Real dataset hashes and durable report resume, with CPU fixture workers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark_core as core
import benchmark_fixtures as fixtures
import benchmark_runner as runner


class TrainingResumeIoTests(unittest.TestCase):
    def prepare(self, root, completed):
        for name in ('first', 'second'):
            dataset = root / name
            dataset.mkdir()
            (dataset / 'one.wav').write_bytes(name.encode())
            (dataset / 'metadata.jsonl').write_text(
                json.dumps({'audio_filepath': 'one.wav', 'text': name}) + '\n')
        manifest = fixtures.build_lora_training_manifest(
            [{'dataset_path': name, 'sample_count': 1} for name in ('first', 'second')],
            str(root), repetitions=2)
        environment = core.build_environment_fingerprint('local', {
            'hostname': 'cpu-fixture', 'gpu_name': 'none', 'backend': 'cpu',
            'python_version': 'fixture', 'git_commit': 'fixture'})
        report = core.build_benchmark_report(manifest, environment)
        for index, repetition in completed:
            report['cases'].append({'fixture_id': manifest['fixtures'][index]['id'],
                                    'repetition': repetition, 'status': 'passed',
                                    'metrics': {'elapsed_seconds': .5}})
        path = root / 'report.json'
        core.save_benchmark_report(str(path), report)
        state = {'cancel': False, 'status': 'running',
                 'tasks': [{'status': 'pending'}, {'status': 'pending'}]}
        return manifest, environment, report, path, state

    def test_completed_resume_reads_no_dataset_and_preserves_report_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, environment, report, path, state = self.prepare(
                root, [(0, 1), (0, 2), (1, 1), (1, 2)])
            before = path.read_bytes()
            original = copy.deepcopy(manifest)
            with patch.object(runner, '_validate_lora_training_fixture',
                              wraps=runner._validate_lora_training_fixture) as validate, \
                 patch.object(runner, '_run_lora_training_worker') as worker, \
                 patch.object(runner, 'load_app_config', return_value={}) as config:
                result = runner.run_lora_training_benchmark(
                    manifest, environment, str(path), state, 'unused', str(root))
            self.assertEqual(0, validate.call_count)
            worker.assert_not_called()
            config.assert_not_called()
            self.assertEqual(report, result)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(original, manifest)
            self.assertEqual('complete', state['status'])
            self.assertEqual(['done', 'done'], [t['status'] for t in state['tasks']])

    def test_partial_resume_hashes_pending_fixture_once_and_keeps_prior_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, environment, report, path, state = self.prepare(root, [(0, 1), (0, 2)])
            # Completed evidence is historical; its source need not be available.
            (root / 'first' / 'one.wav').unlink()
            with patch.object(runner, '_validate_lora_training_fixture',
                              wraps=runner._validate_lora_training_fixture) as validate, \
                 patch.object(runner, '_run_lora_training_worker',
                              return_value={'status': 'passed', 'metrics': {'elapsed_seconds': 1}}) as worker, \
                 patch.object(runner, 'load_app_config', return_value={}):
                result = runner.run_lora_training_benchmark(
                    manifest, environment, str(path), state, 'unused', str(root))
            validate.assert_called_once()
            self.assertEqual('second', validate.call_args.args[0]['dataset_path'])
            self.assertEqual(2, worker.call_count)
            self.assertEqual(report['cases'], result['cases'][:2])
            self.assertEqual(result, json.loads(path.read_text()))

    def test_pending_audio_drift_fails_before_any_worker_or_report_change(self):
        for name, error in (('one.wav', 'training audio hash changed'),
                            ('metadata.jsonl', 'training metadata hash changed')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest, environment, report, path, state = self.prepare(root, [(0, 1), (0, 2), (1, 1)])
                before = path.read_bytes()
                (root / 'second' / name).write_bytes(b'changed')
                with patch.object(runner, '_run_lora_training_worker') as worker:
                    with self.assertRaisesRegex(ValueError, error):
                        runner.run_lora_training_benchmark(
                            manifest, environment, str(path), state, 'unused', str(root))
                worker.assert_not_called()
                self.assertEqual(before, path.read_bytes())

    def test_fresh_run_validates_both_fixtures_once_before_all_repetitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, environment, report, path, state = self.prepare(root, [])
            path.unlink()
            with patch.object(runner, '_validate_lora_training_fixture',
                              wraps=runner._validate_lora_training_fixture) as validate, \
                 patch.object(runner, '_run_lora_training_worker',
                              return_value={'status': 'passed'}) as worker, \
                 patch.object(runner, 'load_app_config', return_value={}):
                result = runner.run_lora_training_benchmark(
                    manifest, environment, str(path), state, 'unused', str(root))
            self.assertEqual(2, validate.call_count)
            self.assertEqual(4, worker.call_count)
            self.assertEqual(result, json.loads(path.read_text()))
            self.assertEqual('complete', state['status'])

    def test_identity_mismatch_rejected_before_completed_fixture_reads(self):
        for mismatch in ('manifest', 'environment'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest, environment, report, path, state = self.prepare(
                    root, [(0, 1), (0, 2), (1, 1), (1, 2)])
                before = path.read_bytes()
                if mismatch == 'manifest':
                    manifest['settings'] = {'different': True}
                else:
                    environment = core.build_environment_fingerprint(
                        'local', {**environment['details'], 'hostname': 'other'})
                with patch.object(runner, '_validate_lora_training_fixture') as validate, \
                     patch.object(runner, '_run_lora_training_worker') as worker:
                    with self.assertRaisesRegex(ValueError, f'benchmark {mismatch} changed'):
                        runner.run_lora_training_benchmark(
                            manifest, environment, str(path), state, 'unused', str(root))
                validate.assert_not_called()
                worker.assert_not_called()
                self.assertEqual(before, path.read_bytes())
