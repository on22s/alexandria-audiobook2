import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alexandria_preparer_rocm_compatible import (
    DETECTION_MIN_DURATION_SECS,
    DETECTION_WINDOW_SECS,
    _is_multi_speaker,
    _plan_detection_windows,
)


def _window(start, end, speaker_seconds):
    return {"start": start, "end": end, "speaker_seconds": speaker_seconds}


class PlanDetectionWindowsTests(unittest.TestCase):
    def test_ten_hour_book_gets_three_spread_windows(self):
        windows = _plan_detection_windows(36000)
        self.assertEqual(3, len(windows))
        self.assertEqual([(5940.0, 6060.0), (17940.0, 18060.0), (29940.0, 30060.0)],
                         windows)
        for start, stop in windows:
            self.assertEqual(DETECTION_WINDOW_SECS, stop - start)

    def test_short_file_returns_empty_plan(self):
        self.assertEqual([], _plan_detection_windows(DETECTION_MIN_DURATION_SECS - 1))

    def test_windows_are_clamped_inside_the_file(self):
        for start, stop in _plan_detection_windows(DETECTION_MIN_DURATION_SECS):
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(stop, DETECTION_MIN_DURATION_SECS)


class IsMultiSpeakerTests(unittest.TestCase):
    def test_dominant_only_window_is_single_speaker(self):
        verdict, evidence = _is_multi_speaker(
            [_window(0, 120, {"SPEAKER_00": 100.0, "SPEAKER_01": 3.0})])
        self.assertFalse(verdict)
        self.assertFalse(evidence[0]["multi_speaker"])

    def test_eighty_twenty_window_is_multi_speaker(self):
        verdict, evidence = _is_multi_speaker(
            [_window(0, 120, {"SPEAKER_00": 80.0, "SPEAKER_01": 20.0})])
        self.assertTrue(verdict)
        self.assertTrue(evidence[0]["multi_speaker"])

    def test_all_single_speaker_windows_is_single(self):
        verdict, _ = _is_multi_speaker([
            _window(0, 120, {"SPEAKER_00": 90.0}),
            _window(500, 620, {"SPEAKER_00": 85.0}),
            _window(900, 1020, {"SPEAKER_00": 95.0}),
        ])
        self.assertFalse(verdict)

    def test_unstable_labels_across_windows_do_not_fake_multi(self):
        # pyannote labels are per-run: the same narrator can be SPEAKER_00 in
        # one window and SPEAKER_01 in another. Per-window decisions must not
        # combine them into a phantom second speaker.
        verdict, _ = _is_multi_speaker([
            _window(0, 120, {"SPEAKER_00": 90.0}),
            _window(500, 620, {"SPEAKER_01": 90.0}),
        ])
        self.assertFalse(verdict)

    def test_any_multi_window_makes_the_file_multi(self):
        verdict, evidence = _is_multi_speaker([
            _window(0, 120, {"SPEAKER_00": 90.0}),
            _window(500, 620, {"SPEAKER_00": 60.0, "SPEAKER_01": 40.0}),
        ])
        self.assertTrue(verdict)
        self.assertEqual([False, True], [w["multi_speaker"] for w in evidence])

    def test_empty_window_is_single_and_safe(self):
        verdict, evidence = _is_multi_speaker([_window(0, 120, {})])
        self.assertFalse(verdict)
        self.assertEqual({}, evidence[0]["speaker_seconds"])


if __name__ == "__main__":
    unittest.main()
