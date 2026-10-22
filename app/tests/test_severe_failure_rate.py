"""Severe-failure flags behind #1520, on signals whose answer is known.

Every signal is synthetic: a tone of known level, gaps of known length, takes
of exactly known sample counts. Each detector has cases it must flag and cases
it must NOT flag, and every "cannot tell" path must come back unmeasured
rather than as a pass.
"""
import os
import tempfile
import unittest

import numpy as np
import soundfile

from experiments import severe_failure_rate as sfr

RATE = 24000


def tone(seconds, level=0.3):
    t = np.arange(int(RATE * seconds)) / RATE
    return level * np.sin(2 * np.pi * 180 * t)


def silence(seconds):
    return np.zeros(int(RATE * seconds))


class CapHitTest(unittest.TestCase):
    def test_exactly_the_cap_is_a_hit(self):
        self.assertTrue(sfr.get_cap_hit(2048 * 1920, RATE, 2048))

    def test_one_token_short_is_still_a_hit(self):
        self.assertTrue(sfr.get_cap_hit(2047 * 1920, RATE, 2048))

    def test_two_tokens_short_is_not(self):
        self.assertFalse(sfr.get_cap_hit(2046 * 1920, RATE, 2048))

    def test_a_normal_line_is_not(self):
        self.assertFalse(sfr.get_cap_hit(int(3.2 * RATE), RATE, 2048))

    def test_windowed_takes_are_unmeasured(self):
        self.assertIsNone(sfr.get_cap_hit(2048 * 1920, RATE, 2048, windowed=True))

    def test_a_foreign_sample_rate_is_unmeasured(self):
        self.assertIsNone(sfr.get_cap_hit(2048 * 1920, 22050, 2048))

    def test_an_unknown_cap_is_unmeasured(self):
        self.assertIsNone(sfr.get_cap_hit(2048 * 1920, RATE, None))


class SilenceTest(unittest.TestCase):
    def test_long_internal_gap_is_flagged(self):
        flags = sfr.get_silence_flags(np.concatenate([tone(1), silence(2.0), tone(1)]), RATE)
        self.assertTrue(flags["internal_silence"])
        self.assertAlmostEqual(flags["longest_internal_silence_s"], 2.0, delta=0.02)
        self.assertFalse(flags["trailing_silence"])

    def test_short_internal_gap_is_not(self):
        flags = sfr.get_silence_flags(np.concatenate([tone(1), silence(1.0), tone(1)]), RATE)
        self.assertFalse(flags["internal_silence"])
        self.assertAlmostEqual(flags["longest_internal_silence_s"], 1.0, delta=0.02)

    def test_long_tail_is_flagged(self):
        flags = sfr.get_silence_flags(np.concatenate([tone(1), silence(1.5)]), RATE)
        self.assertTrue(flags["trailing_silence"])
        self.assertAlmostEqual(flags["trailing_silence_s"], 1.5, delta=0.02)
        self.assertFalse(flags["internal_silence"])

    def test_short_tail_is_not(self):
        flags = sfr.get_silence_flags(np.concatenate([tone(1), silence(0.3)]), RATE)
        self.assertFalse(flags["trailing_silence"])

    def test_all_zeros_is_all_silent_not_measured_as_pauses(self):
        flags = sfr.get_silence_flags(silence(2), RATE)
        self.assertTrue(flags["all_silent"])
        self.assertIsNone(flags["trailing_silence_s"])
        self.assertIsNone(flags["longest_internal_silence_s"])

    def test_a_quiet_tone_above_the_threshold_is_not_silence(self):
        # -40 dBFS RMS is above the -45 dBFS export-trim threshold.
        quiet = tone(1, level=10 ** (-40 / 20) * np.sqrt(2))
        flags = sfr.get_silence_flags(np.concatenate([tone(1), quiet, tone(1)]), RATE)
        self.assertFalse(flags["all_silent"])
        self.assertLess(flags["longest_internal_silence_s"], 0.05)

    def test_threshold_is_the_shared_export_one(self):
        from tts import EDGE_SILENCE_THRESHOLD_DBFS
        just_below = tone(2, level=10 ** ((EDGE_SILENCE_THRESHOLD_DBFS - 1) / 20) * np.sqrt(2))
        flags = sfr.get_silence_flags(np.concatenate([tone(1), just_below, tone(1)]), RATE)
        self.assertTrue(flags["internal_silence"])


class TakeAndReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, samples):
        soundfile.write(os.path.join(self.dir.name, name), samples, RATE)
        return name

    def test_missing_audio_is_unmeasured_on_every_reason(self):
        flags = sfr.get_take_flags({"uid": "u1", "audio_path": "nope.wav"}, self.dir.name, 2048, False)
        self.assertTrue(flags["audio"].startswith("unreadable"))
        self.assertTrue(all(flags[reason] is None for reason in sfr.REASONS))

    def test_take_without_a_receipt_has_unmeasured_content(self):
        path = self.write("a.wav", np.concatenate([tone(1), silence(2.0), tone(1)]))
        flags = sfr.get_take_flags({"uid": "u1", "audio_path": path}, self.dir.name, 2048, False)
        self.assertEqual(flags["content_status"], "unmeasured")
        self.assertIsNone(flags["content"])
        self.assertTrue(flags["internal_silence"])
        self.assertFalse(flags["cap_hit"])

    def test_union_counts_a_take_with_two_reasons_once(self):
        rows = [{"content": None, "all_silent": False, "cap_hit": True,
                 "trailing_silence": True, "internal_silence": False},
                {"content": None, "all_silent": False, "cap_hit": False,
                 "trailing_silence": False, "internal_silence": False}]
        report = sfr.get_report(rows)
        self.assertEqual(report["severe"], 1)
        self.assertEqual(report["severe_rate"], 0.5)
        self.assertEqual(report["per_reason"]["cap_hit"], 1)
        self.assertEqual(report["per_reason"]["trailing_silence"], 1)
        self.assertEqual(report["unmeasured"]["content"], 2)

    def test_unmeasured_never_counts_as_severe_or_as_clean(self):
        report = sfr.get_report([{reason: None for reason in sfr.REASONS}])
        self.assertEqual(report["severe"], 0)
        self.assertEqual(report["unmeasured"]["cap_hit"], 1)


if __name__ == "__main__":
    unittest.main()
