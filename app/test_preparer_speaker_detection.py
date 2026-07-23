import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FAKE_TORCH = types.ModuleType("torch")
_FAKE_TORCH.from_numpy = lambda values: values

from alexandria_preparer_rocm_compatible import (
    DETECTION_MIN_DURATION_SECS,
    DETECTION_WINDOW_SECS,
    DIARIZATION_MODEL_ID,
    _assign_speakers_to_words,
    _is_multi_speaker,
    _plan_detection_windows,
    diarize_audio,
    get_diarization_audio_input,
    get_speaker_diarization,
)


class DiarizationModelTests(unittest.TestCase):
    def test_production_model_is_community_one(self):
        self.assertEqual(
            "pyannote/speaker-diarization-community-1",
            DIARIZATION_MODEL_ID,
        )


class _FakeInterval:
    def __init__(self, start, end, data):
        self.start = start
        self.end = end
        self.data = data


class _FakeIntervalTree:
    def __init__(self):
        self.intervals = []

    def add(self, interval):
        self.intervals.append(interval)

    def at(self, point):
        return [
            interval for interval in self.intervals
            if interval.start <= point < interval.end
        ]


def _window(start, end, speaker_seconds):
    return {"start": start, "end": end, "speaker_seconds": speaker_seconds}


class AssignSpeakersToWordsTests(unittest.TestCase):
    def assign(self, words, speakers):
        with patch(
            "alexandria_preparer_rocm_compatible._lazy_import_intervaltree",
            return_value=(_FakeIntervalTree, _FakeInterval),
        ):
            return _assign_speakers_to_words(words, speakers)

    def test_word_inside_segment_keeps_direct_assignment(self):
        words, speakers = self.assign(
            [{"start": 1.2, "end": 1.4}],
            [{"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}],
        )
        self.assertEqual("SPEAKER_00", words[0]["speaker"])
        self.assertEqual({"SPEAKER_00"}, speakers)

    def test_short_gap_between_same_speaker_is_bridged(self):
        words, speakers = self.assign(
            [{"start": 1.35, "end": 1.55}],
            [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                {"start": 1.9, "end": 3.0, "speaker": "SPEAKER_00"},
            ],
        )
        self.assertEqual("SPEAKER_00", words[0]["speaker"])
        self.assertEqual({"SPEAKER_00"}, speakers)

    def test_word_too_far_from_one_flank_stays_unknown(self):
        words, speakers = self.assign(
            [{"start": 1.05, "end": 1.15}],
            [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_00"},
            ],
        )
        self.assertEqual("UNKNOWN", words[0]["speaker"])
        self.assertEqual({"UNKNOWN"}, speakers)

    def test_short_gap_between_different_speakers_stays_unknown(self):
        words, speakers = self.assign(
            [{"start": 1.15, "end": 1.25}],
            [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                {"start": 1.4, "end": 2.0, "speaker": "SPEAKER_01"},
            ],
        )
        self.assertEqual("UNKNOWN", words[0]["speaker"])
        self.assertEqual({"UNKNOWN"}, speakers)

    def test_one_sided_gap_stays_unknown(self):
        words, speakers = self.assign(
            [{"start": 1.05, "end": 1.15}],
            [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
        )
        self.assertEqual("UNKNOWN", words[0]["speaker"])
        self.assertEqual({"UNKNOWN"}, speakers)


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


class GetSpeakerDiarizationTests(unittest.TestCase):
    def test_prefers_exclusive_timeline_for_word_alignment(self):
        output = type("Output", (), {
            "speaker_diarization": "regular",
            "exclusive_speaker_diarization": "exclusive",
        })()
        self.assertEqual(
            "exclusive",
            get_speaker_diarization(output, prefer_exclusive=True),
        )

    def test_sampled_detection_uses_regular_timeline(self):
        output = type("Output", (), {
            "speaker_diarization": "regular",
            "exclusive_speaker_diarization": "exclusive",
        })()
        self.assertEqual("regular", get_speaker_diarization(output))

    def test_missing_exclusive_timeline_falls_back_to_regular(self):
        output = type("Output", (), {"speaker_diarization": "regular"})()
        self.assertEqual(
            "regular",
            get_speaker_diarization(output, prefer_exclusive=True),
        )

    def test_missing_regular_timeline_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "no speaker diarization"):
            get_speaker_diarization(object())

    def test_full_diarization_emits_segments_from_exclusive_timeline(self):
        turn = type("Turn", (), {"start": 1.25, "end": 2.5})()

        class Annotation:
            def __init__(self, speaker):
                self.speaker = speaker

            def itertracks(self, yield_label=False):
                self.assert_yield_label = yield_label
                yield turn, None, self.speaker

        output = type("Output", (), {
            "speaker_diarization": Annotation("regular"),
            "exclusive_speaker_diarization": Annotation("exclusive"),
        })()
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "audio.wav")
            sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)
            received = []

            def pipeline(audio_input):
                received.append(audio_input)
                return output

            with patch.dict(sys.modules, {"torch": _FAKE_TORCH}):
                segments = diarize_audio(audio_path, pipeline=pipeline)

        self.assertEqual(
            [{"start": 1.25, "end": 2.5, "speaker": "exclusive"}],
            segments,
        )
        self.assertEqual(16000, received[0]["sample_rate"])
        self.assertEqual((1, 1600), tuple(received[0]["waveform"].shape))

    def test_diarization_audio_input_preserves_stereo_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "stereo.wav")
            audio = np.column_stack((
                np.full(800, 0.25, dtype=np.float32),
                np.full(800, -0.25, dtype=np.float32),
            ))
            sf.write(audio_path, audio, 8000)

            with patch.dict(sys.modules, {"torch": _FAKE_TORCH}):
                decoded = get_diarization_audio_input(audio_path)

        self.assertEqual(8000, decoded["sample_rate"])
        self.assertEqual((2, 800), tuple(decoded["waveform"].shape))
        self.assertAlmostEqual(0.25, decoded["waveform"][0, 0].item(), places=3)
        self.assertAlmostEqual(-0.25, decoded["waveform"][1, 0].item(), places=3)


if __name__ == "__main__":
    unittest.main()
