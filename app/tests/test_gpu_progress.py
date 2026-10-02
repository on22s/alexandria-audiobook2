"""Hand-calculated timing cases, including resumed work and rejected evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gpu_progress as progress

IDENTITY = {'run_id': 'new-run', 'owner_pid': '123', 'owner_token': 'boot:456', 'job': 'fixture'}


class GpuProgressTests(unittest.TestCase):
    def document(self, pairs, total=20):
        return {**IDENTITY, 'phase': 'audio rows', 'total': total,
                'samples': [{'completed': count, 'monotonic': timestamp} for count, timestamp in pairs]}

    def test_known_uneven_intervals_report_average_and_spread(self):
        # 4 units/8s and 2 units/12s: 2..6s per unit, mean20/6.
        result = progress.get_gpu_progress_estimate(self.document([(0, 100), (4, 108), (6, 120)]), IDENTITY)
        self.assertEqual(14, result['remaining'])
        self.assertAlmostEqual(140 / 3, result['seconds'])
        self.assertEqual((28, 84), (result['seconds_low'], result['seconds_high']))
        self.assertEqual(2, result['measured_intervals'])

    def test_resumed_completed_units_are_not_measured_as_new_work(self):
        result = progress.get_gpu_progress_estimate(self.document([(15, 100), (17, 110)]), IDENTITY)
        self.assertEqual(3, result['remaining'])
        self.assertEqual(15, result['seconds'])
        self.assertEqual(5, result['seconds_per_unit'])

    def test_wrong_invocation_pid_or_birth_identity_is_rejected(self):
        for key in IDENTITY:
            with self.subTest(key=key):
                document = self.document([(0, 1), (1, 2)])
                document[key] = 'old'
                with self.assertRaisesRegex(ValueError, 'identity'):
                    progress.get_gpu_progress_estimate(document, IDENTITY)

    def test_unknown_invalid_or_non_advancing_samples_are_rejected(self):
        cases = ([], [(4, 10)], [(4, 10), (4, 20)], [(4, 10), (3, 20)],
                 [(4, 10), (5, 10)], [(4, 10), (5, 9)], [(4, 10), (5, float('nan'))],
                 [(False, 10), (5, 20)], [(4, 10), (21, 20)], [(0, 0), (1, 1e308)])
        for pairs in cases:
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                progress.get_gpu_progress_estimate(self.document(pairs), IDENTITY)

    def test_actual_published_artifact_resets_phase_without_carrying_old_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'progress.json'
            progress.save_gpu_progress(path, IDENTITY)
            environment = {'GPU_PROGRESS_FILE': str(path),
                           'GPU_PROGRESS_LIBRARY': str(Path(__file__).resolve().parents[2] / 'run_chains/lib/gpu_pending.sh'),
                           **dict(zip(
                ('GPU_PROGRESS_RUN_ID', 'GPU_PROGRESS_OWNER_PID', 'GPU_PROGRESS_OWNER_TOKEN', 'GPU_PROGRESS_JOB'),
                IDENTITY.values()))}
            with patch.dict(os.environ, environment), patch.object(progress.time, 'monotonic', side_effect=[100, 110, 200, 220]):
                progress.record_gpu_progress('generation', 15, 20)
                progress.record_gpu_progress('generation', 17, 20)
                before = json.loads(path.read_text())
                self.assertEqual(15, progress.get_gpu_progress_estimate(before, IDENTITY)['seconds'])
                progress.record_gpu_progress('scoring', 0, 10)
                with self.assertRaisesRegex(ValueError, 'Not enough'):
                    progress.get_gpu_progress_estimate(json.loads(path.read_text()), IDENTITY)
                progress.record_gpu_progress('scoring', 2, 10)
                after = json.loads(path.read_text())
                self.assertEqual(80, progress.get_gpu_progress_estimate(after, IDENTITY)['seconds'])
            self.assertNotEqual(before, after)
            self.assertEqual([], list(Path(tmp).glob('progress.json.*')))

    def test_uninstrumented_process_creates_no_progress_artifact(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(progress, 'save_gpu_progress') as write:
            progress.record_gpu_progress('rows', 0, 10)
            write.assert_not_called()

    def test_new_worker_same_phase_starts_fresh_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'progress.json'
            progress.save_gpu_progress(path, IDENTITY)
            environment = {'GPU_PROGRESS_FILE': str(path), 'GPU_PROGRESS_LIBRARY': 'fixture-library',
                           **dict(zip(('GPU_PROGRESS_RUN_ID', 'GPU_PROGRESS_OWNER_PID',
                                       'GPU_PROGRESS_OWNER_TOKEN', 'GPU_PROGRESS_JOB'), IDENTITY.values()))}
            with patch.dict(os.environ, environment), patch.object(progress, '_WORKER_TOKENS', {}), \
                 patch.object(progress.os, 'getpid', return_value=12345) as worker_pid, \
                 patch.object(progress, 'get_gpu_progress_process_token', side_effect=['birth1', 'birth2']), \
                 patch.object(progress.time, 'monotonic', side_effect=[100, 110, 200]):
                progress.record_gpu_progress('rows', 0, 10)
                progress.record_gpu_progress('rows', 5, 10)
                self.assertEqual(10, progress.get_gpu_progress_estimate(json.loads(path.read_text()), IDENTITY)['seconds'])
                worker_pid.return_value = 12346
                progress.record_gpu_progress('rows', 0, 10)
            document = json.loads(path.read_text())
            self.assertEqual('12346', document['worker_pid'])
            self.assertEqual([{'completed': 0, 'monotonic': 200}], document['samples'])
            with self.assertRaisesRegex(ValueError, 'Not enough'):
                progress.get_gpu_progress_estimate(document, IDENTITY)
