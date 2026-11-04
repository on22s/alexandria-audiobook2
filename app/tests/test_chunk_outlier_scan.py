import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chunk_outlier_check import check_chunk_outliers, OutlierCheckCancelled, OutlierCheckError


class OutlierScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rows = [dict(id=index, uid=str(index), speaker='A', text='Synthetic',
                          status='done', audio_path=f'{index}.wav') for index in range(16)]
        for row in self.rows:
            (self.root / row['audio_path']).write_bytes(b'synthetic')
        self.snapshot = dict(root=str(self.root), chunks=self.rows, book={'voices': {}})
        self.measurement = dict(loudness_db=-20., syllable_rate=3., median_pitch_hz=120.,
                                voiced_ratio=.9, error=None)

    def decode(self, path, check_active):
        check_active()
        return dict(self.measurement)

    def test_full_population_one_decode_per_take_and_no_input_mutation(self):
        before = copy.deepcopy(self.snapshot)
        progress = []
        with patch('chunk_outliers.decode_take_acoustics', side_effect=self.decode) as decode:
            results = check_chunk_outliers(self.snapshot, lambda: None, lambda *args: progress.append(args))
        self.assertEqual(16, decode.call_count)
        self.assertEqual((16, 16), progress[-1])
        self.assertEqual(before, self.snapshot)
        self.assertEqual(list(range(16)), [item['index'] for item in results])
        self.assertTrue(all(item['error'] is None and not item['flagged'] for item in results))

    def test_different_characters_do_not_create_a_comparison_population(self):
        for index, row in enumerate(self.rows):
            row['speaker'] = 'A' if index < 7 else f'Other{index}'
        with patch('voice_drift.get_drift_voice_groups', return_value=({}, dict.fromkeys(range(16), 'same'))):
            results = check_chunk_outliers(self.snapshot, lambda: None, decode=self.decode)
        self.assertTrue(all(item['error'] and not item['flagged'] for item in results))

    def test_effective_voice_timeline_states_are_not_pooled(self):
        self.snapshot['book']['voices'] = {'A': {'type': 'custom', 'voice': 'before',
            'versions': {'other': {'type': 'custom', 'voice': 'after'}},
            'version_timeline': [{'from_index': 7, 'version_id': 'other'}]}}
        results = check_chunk_outliers(self.snapshot, lambda: None, decode=self.decode)
        self.assertTrue(results[0]['error'])
        self.assertIsNone(results[7]['error'])

    def test_outside_audio_is_never_decoded_and_missing_stays_unavailable(self):
        self.rows[0]['audio_path'] = '../outside.wav'
        (self.root / self.rows[1]['audio_path']).unlink()
        with patch('chunk_outliers.decode_take_acoustics', side_effect=self.decode) as decode:
            results = check_chunk_outliers(self.snapshot, lambda: None)
        self.assertEqual(14, decode.call_count)
        self.assertTrue(results[0]['error'] and results[1]['error'])

    def test_cancel_deadline_population_limit_and_changed_take_abort(self):
        def cancel():
            raise OutlierCheckCancelled('cancelled')
        with self.assertRaises(OutlierCheckCancelled):
            check_chunk_outliers(self.snapshot, cancel, decode=self.decode)
        with patch('chunk_outlier_check.time.monotonic', side_effect=[0, 601]):
            with self.assertRaisesRegex(OutlierCheckError, 'deadline'):
                check_chunk_outliers(self.snapshot, lambda: None, decode=self.decode)
        with patch('chunk_outlier_check.MAX_SCAN_CHUNKS', 1):
            with self.assertRaisesRegex(OutlierCheckError, 'at most 1 saved lines'):
                check_chunk_outliers(self.snapshot, lambda: None, decode=self.decode)
        def change_take(path, check_active):
            Path(path).write_bytes(b'changed synthetic take')
            return self.decode(path, check_active)
        with self.assertRaisesRegex(ValueError, 'take changed'):
            check_chunk_outliers(self.snapshot, lambda: None, decode=change_take)

    def test_unsafe_paths_rejected_before_realpath(self):
        for relative in (42, ['bad'], r'\\server\share\take.wav', r'C:take.wav',
                         r'C:\take.wav', '/take.wav', r'\take.wav', '../outside.wav'):
            with self.subTest(path=relative):
                self.rows[:] = [dict(self.rows[0], audio_path=relative)]
                with patch('chunk_outlier_check.os.path.realpath', side_effect=[str(self.root)]) as realpath:
                    result = check_chunk_outliers(self.snapshot, lambda: None, decode=self.decode)
                self.assertEqual(1, realpath.call_count)
                self.assertTrue(result[0]['error'])

    def test_vanishing_take_is_unavailable(self):
        def vanish(path, check_active):
            Path(path).unlink()
            return self.decode(path, check_active)
        results = check_chunk_outliers(self.snapshot, lambda: None, decode=vanish)
        self.assertTrue(all(result['error'] for result in results))

    def test_bounded_parallel_workers_join_on_cancellation(self):
        gate = threading.Barrier(4, timeout=5)
        cancel = threading.Event()
        lock = threading.Lock()
        active = 0
        started = 0

        def decode(path, check_active):
            nonlocal active, started
            with lock:
                active += 1
                started += 1
            try:
                gate.wait()
                cancel.set()
                while True:
                    check_active()
                    time.sleep(.001)
            finally:
                with lock:
                    active -= 1

        def check_active():
            if cancel.is_set():
                raise OutlierCheckCancelled('cancelled')

        with self.assertRaises(OutlierCheckCancelled):
            check_chunk_outliers(self.snapshot, check_active, decode=decode)
        self.assertEqual(4, started)
        self.assertEqual(0, active)
