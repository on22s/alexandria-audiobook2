"""Severe-failure flags behind #1520, on signals whose answer is known.

Every signal is synthetic: a tone of known level, gaps of known length, takes
of exactly known sample counts. Each detector has cases it must flag and cases
it must NOT flag, and every "cannot tell" path must come back unmeasured
rather than as a pass.
"""
import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_the_measured_hit_length_is_flagged(self):
        # Real takes forced to cap 40 were exactly 74,880 frames (39 x 1920).
        self.assertTrue(sfr.get_cap_hit(74880, RATE, 40))
        self.assertFalse(sfr.get_cap_hit(74880, RATE, 40 + 2))

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

    def test_invalid_calibration_caps_are_unmeasured(self):
        for cap in (True, 0, 1, -1, 40.0, "40"):
            with self.subTest(cap=cap):
                self.assertIsNone(sfr.get_cap_hit(74880, RATE, cap))


class SilenceTest(unittest.TestCase):
    def test_short_audible_final_bin_is_not_diluted_by_padding(self):
        # 1ms at -40dBFS becomes -50dBFS if padded to a 10ms bin.
        for values in (np.full(24, 0.01), np.column_stack([np.full(24, 0.01), np.zeros(24)])):
            flags = sfr.get_silence_flags(values, RATE)
            self.assertFalse(flags["all_silent"])
            self.assertEqual(flags["trailing_silence_s"], 0)

    def test_partial_quiet_tail_does_not_round_up_to_the_failure_threshold(self):
        flags = sfr.get_silence_flags(np.concatenate([tone(1), silence(1.191)]), RATE)
        self.assertEqual(flags["trailing_silence_s"], 1.191)
        self.assertFalse(flags["trailing_silence"])

    def test_short_silent_clip_reports_only_its_real_duration(self):
        flags = sfr.get_silence_flags(np.zeros(24), RATE)
        self.assertTrue(flags["all_silent"])
        self.assertEqual(flags["leading_silence_s"], 0.001)

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
        flags = sfr.get_take_flags({"uid": "u1", "audio_path": "nope.wav"}, self.dir.name)
        self.assertTrue(flags["audio"].startswith("unreadable"))
        self.assertTrue(all(flags[reason] is None for reason in sfr.REASONS))

    def test_take_without_a_receipt_has_unmeasured_content(self):
        path = self.write("a.wav", np.concatenate([tone(1), silence(2.0), tone(1)]))
        flags = sfr.get_take_flags({"uid": "u1", "audio_path": path}, self.dir.name)
        self.assertEqual(flags["content_status"], "unmeasured")
        self.assertIsNone(flags["content"])
        self.assertTrue(flags["internal_silence"])
        self.assertIsNone(flags["cap_hit"])

    def test_real_take_flags_serialize_to_json(self):
        # The report is written with json; numpy scalars once broke it.
        import json
        path = self.write("b.wav", np.concatenate([tone(1), silence(2.0), tone(1), silence(1.5)]))
        flags = sfr.get_take_flags({"uid": "u2", "audio_path": path}, self.dir.name)
        report = sfr.get_report([flags])
        json.dumps({"rows": [flags], "summary": report})
        self.assertIs(type(flags["internal_silence"]), bool)
        self.assertIs(type(flags["trailing_silence"]), bool)
        self.assertIsNone(flags["cap_hit"])

    def test_antiphase_stereo_is_audible_in_both_channels(self):
        wave = tone(2)
        path = self.write("stereo.wav", np.column_stack([wave, -wave]))
        flags = sfr.get_take_flags({"uid": "stereo", "audio_path": path}, self.dir.name)
        self.assertFalse(flags["all_silent"])
        self.assertFalse(flags["internal_silence"])
        self.assertEqual(flags["trailing_silence_s"], 0)

    def test_antiphase_section_is_not_an_internal_silence(self):
        wave = tone(4)
        right = wave.copy()
        right[RATE:3 * RATE] *= -1
        path = self.write("section.wav", np.column_stack([wave, right]))
        flags = sfr.get_take_flags({"audio_path": path}, self.dir.name)
        self.assertFalse(flags["internal_silence"])

    def test_one_quiet_audible_channel_is_enough(self):
        wave = tone(2, level=10 ** (-44.5 / 20) * np.sqrt(2))
        for channels in ([wave, np.zeros_like(wave)], [np.zeros_like(wave), wave]):
            flags = sfr.get_silence_flags(np.column_stack(channels), RATE)
            self.assertFalse(flags["all_silent"])

    def test_real_stereo_gap_and_tail_still_flagged(self):
        wave = np.concatenate([tone(1), silence(2), tone(1), silence(1.5)])
        path = self.write("gaps.wav", np.column_stack([wave, -wave]))
        flags = sfr.get_take_flags({"audio_path": path}, self.dir.name)
        self.assertTrue(flags["internal_silence"])
        self.assertTrue(flags["trailing_silence"])
        self.assertAlmostEqual(flags["longest_internal_silence_s"], 2, delta=0.02)

    def test_book_cli_never_applies_current_cap_to_old_or_external_audio(self):
        path = self.write("old.wav", tone(4))
        chunks = [{"uid": "windowed", "audio_path": path,
                   "synthesis_receipt": {"window_count": 2, "output_frames": 96000}},
                  {"uid": "external", "audio_path": path},
                  {"uid": "missing"}]
        chunks_path = os.path.join(self.dir.name, "chunks.json")
        config_path = os.path.join(self.dir.name, "config.json")
        out_path = os.path.join(self.dir.name, "report.json")
        with open(chunks_path, "w") as handle:
            json.dump(chunks, handle)
        summaries = []
        for cap, windows in ((40, False), (2048, True)):
            with open(config_path, "w") as handle:
                json.dump({"tts": {"max_new_tokens": cap, "synthesis_windows": {"enabled": windows}}}, handle)
            argv = ["severe_failure_rate", "--chunks", chunks_path, "--audio-root", self.dir.name,
                    "--config", config_path, "--out", out_path]
            with patch.object(sys, "argv", argv), patch("experiments.provenance.provenance", return_value={}):
                sfr.main()
            with open(out_path) as handle:
                report = json.load(handle)
            self.assertEqual(report["takes_without_audio"], 1)
            self.assertEqual(report["summary"]["unmeasured"]["cap_hit"], 2)
            self.assertTrue(all(row["cap_hit"] is None for row in report["rows"]))
            summaries.append(report["summary"])
        self.assertEqual(summaries[0], summaries[1])

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
