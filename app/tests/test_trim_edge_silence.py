"""Dead air around each generated line is cut at join time (tts.trim_edge_silence).
Real numbers behind it: 310-340 ms median leading silence on LoRA-path lines,
which sat on top of the configured pause."""
import unittest

from pydub import AudioSegment
from pydub.generators import Sine

import tts


def line(lead_ms, voiced_ms, tail_ms):
    return (AudioSegment.silent(duration=lead_ms)
            + Sine(220).to_audio_segment(duration=voiced_ms).apply_gain(-6)
            + AudioSegment.silent(duration=tail_ms))


class TrimTests(unittest.TestCase):
    def test_leading_and_trailing_silence_are_cut_to_the_keep(self):
        trimmed = tts.trim_edge_silence(line(340, 500, 200))
        self.assertAlmostEqual(len(trimmed), 40 + 500 + 80, delta=25)

    def test_a_line_with_no_dead_air_is_untouched(self):
        seg = line(0, 500, 0)
        self.assertEqual(len(seg), len(tts.trim_edge_silence(seg)))

    def test_a_silent_line_is_not_deleted(self):
        seg = AudioSegment.silent(duration=900)
        self.assertEqual(900, len(tts.trim_edge_silence(seg)))

    def test_short_edges_are_kept_not_extended(self):
        seg = line(20, 500, 30)
        self.assertEqual(len(seg), len(tts.trim_edge_silence(seg)))

    def test_the_configured_pause_is_now_the_gap_the_listener_hears(self):
        a, b = line(340, 400, 80), line(300, 400, 80)
        combined = tts.combine_audio_with_pauses([a, b], ["X", "X"], same_speaker_pause_ms=250)
        # 40+400+80  +250+  40+400+80
        self.assertAlmostEqual(len(combined), 520 + 250 + 520, delta=40)
        timeline = tts.compute_timeline([({"speaker": "X"}, a), ({"speaker": "X"}, b)],
                                        same_speaker_pause_ms=250)
        self.assertAlmostEqual(timeline[1][2], 520 + 250, delta=30)
        self.assertAlmostEqual(len(timeline[1][1]), 520, delta=25)
