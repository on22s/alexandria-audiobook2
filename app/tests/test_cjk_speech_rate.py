"""Tests for cjk_speech_rate.py - synthetic data only, no TTS, no GPU."""
import os
import sys
import unittest

import numpy as np

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.join(APP_DIR, "experiments"))

import cjk_speech_rate  # noqa: E402


class GetCleanTextTests(unittest.TestCase):
    def test_removes_japanese_ruby_and_notation(self):
        raw = "\u5f7c\u306f\uff5c\u6771\u4eac\u300a\u3068\u3046\u304d\u3087\u3046\u300b" \
              "\u306b\uff3b\uff03\u6539\u30da\u30fc\u30b8\uff3d\u4f4f\u3080\u3002"
        self.assertEqual(cjk_speech_rate.get_clean_text(raw, "ja"),
                         "\u5f7c\u306f\u6771\u4eac\u306b\u4f4f\u3080\u3002")

    def test_removes_every_ruby_span(self):
        raw = "\uff5c\u6f22\u5b57\u300a\u304b\u3093\u3058\u300b\u3068\uff5c\u4eee\u540d\u300a\u304b\u306a\u300b"
        self.assertEqual(cjk_speech_rate.get_clean_text(raw, "ja"),
                         "\u6f22\u5b57\u3068\u4eee\u540d")

    def test_keeps_ruby_marks_for_chinese(self):
        raw = "\u4ed6\u3000\u8bf4  \uff5c\u4e1c\u300ading\u300b"
        self.assertEqual(cjk_speech_rate.get_clean_text(raw, "zh"),
                         "\u4ed6 \u8bf4 \uff5c\u4e1c\u300ading\u300b")

    def test_collapses_whitespace(self):
        raw = "One\n\n  two\tthree "
        self.assertEqual(cjk_speech_rate.get_clean_text(raw, "en"),
                         "One two three")


class PickSentencesTests(unittest.TestCase):
    def test_chinese_keeps_only_sentences_in_range(self):
        text = ("\u592a\u77ed\u3002"
                "\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"
                "\u8fd9\u4e00\u4e2a\u53e5\u5b50\u5b9e\u5728\u662f\u592a\u957f\u4e86"
                + "\u554a" * 100 + "\u3002")
        picked = cjk_speech_rate.pick_sentences(text, "zh", 5)
        self.assertEqual(
            picked, ["\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"])

    def test_japanese_split_keeps_terminal_marks(self):
        text = ("\u3053\u308c\u306f\u77ed\u3044\u3002"
                "\u3053\u308c\u306f\u3061\u3087\u3046\u3069\u3044\u3044\u9577\u3055\u306e\u6587\u3067\u3059\uff01")
        self.assertEqual(
            cjk_speech_rate.pick_sentences(text, "ja", 5),
            ["\u3053\u308c\u306f\u3061\u3087\u3046\u3069\u3044\u3044\u9577\u3055\u306e\u6587\u3067\u3059\uff01"])

    def test_chinese_drops_latin_sentences(self):
        text = ("\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"
                "This sentence has Latin letters in it.")
        self.assertEqual(
            cjk_speech_rate.pick_sentences(text, "zh", 5),
            ["\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"])

    def test_english_drops_cjk_sentences(self):
        cjk_sentence = (
            "\u8fd9\u662f\u4e00\u4e2a\u7528\u6765\u6d4b\u8bd5\u4e2d\u6587\u8fc7\u6ee4\u7684\u53e5\u5b50"
            "\u56e0\u4e3a\u5b83\u5fc5\u987b\u8db3\u591f\u957f\u624d\u80fd\u901a\u8fc7\u82f1\u6587\u7684"
            "\u957f\u5ea6\u8303\u56f4\u9650\u5236\u6240\u4ee5\u518d\u8865\u5145\u51e0\u4e2a\u5b57\u3002")
        text = ("A perfectly ordinary English sentence for the test. "
                + cjk_sentence)
        self.assertEqual(
            cjk_speech_rate.pick_sentences(text, "en", 5),
            ["A perfectly ordinary English sentence for the test."])

    def test_english_split_keeps_terminal_marks(self):
        text = ("First one is short. Second sentence here is long enough "
                "to be kept in this test!")
        self.assertEqual(
            cjk_speech_rate.pick_sentences(text, "en", 5),
            ["Second sentence here is long enough to be kept in this test!"])

    def test_picks_evenly_spaced_deterministically(self):
        sentences = ["\u8fd9%s\u7b2c%d\u53e5\u3002" % ("\u554a" * 10, i)
                     for i in range(10)]
        text = "".join(sentences)
        first = cjk_speech_rate.pick_sentences(text, "zh", 3)
        second = cjk_speech_rate.pick_sentences(text, "zh", 3)
        self.assertEqual(first, second)
        self.assertEqual(first, [sentences[0], sentences[4], sentences[9]])

    def test_returns_all_when_fewer_candidates_than_requested(self):
        text = "\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"
        self.assertEqual(cjk_speech_rate.pick_sentences(text, "zh", 5),
                         [text])

    def test_zero_count_returns_empty(self):
        text = "\u8fd9\u662f\u4e00\u4e2a\u957f\u5ea6\u5408\u9002\u7684\u53e5\u5b50\u3002"
        self.assertEqual(cjk_speech_rate.pick_sentences(text, "zh", 0), [])

    def test_explicit_range_overrides_language_defaults(self):
        text = "\u77ed\u53e5\u3002\u4e2d\u7b49\u957f\u5ea6\u7684\u53e5\u5b50\u3002"
        self.assertEqual(cjk_speech_rate.pick_sentences(text, "zh", 5, 3, 6),
                         ["\u77ed\u53e5\u3002"])

    def test_even_indices_rejects_empty_ranges(self):
        self.assertEqual(cjk_speech_rate.get_even_indices(0, 3), [])
        self.assertEqual(cjk_speech_rate.get_even_indices(5, 0), [])


class GetSpeechSecondsTests(unittest.TestCase):
    RATE = 1000

    def _tone(self, amplitude, samples):
        index = np.arange(samples)
        return amplitude * np.sin(2 * np.pi * 50 * index / self.RATE)

    def test_measures_span_between_silences(self):
        samples = np.concatenate([np.zeros(200), self._tone(0.5, 500),
                                  np.zeros(300)])
        self.assertAlmostEqual(
            cjk_speech_rate.get_speech_seconds(samples, self.RATE),
            0.5, places=6)

    def test_pure_silence_is_none(self):
        self.assertIsNone(
            cjk_speech_rate.get_speech_seconds(np.zeros(1000), self.RATE))

    def test_empty_audio_is_none(self):
        self.assertIsNone(
            cjk_speech_rate.get_speech_seconds(np.zeros(0), self.RATE))

    def test_rejects_non_positive_sample_rate(self):
        self.assertIsNone(
            cjk_speech_rate.get_speech_seconds(np.ones(100), 0))

    def test_excludes_tail_more_than_40_db_down(self):
        samples = np.concatenate([np.zeros(100), self._tone(0.5, 500),
                                  self._tone(0.002, 500), np.zeros(100)])
        self.assertAlmostEqual(
            cjk_speech_rate.get_speech_seconds(samples, self.RATE),
            0.5, places=2)


class GetRateSummaryTests(unittest.TestCase):
    def test_summary_math_and_ratio(self):
        rows = [{"language": "en", "voice": "Ryan", "chars": 10,
                 "seconds": 1.0},
                {"language": "en", "voice": "Ryan", "chars": 20,
                 "seconds": 2.0},
                {"language": "zh", "voice": "Ryan", "chars": 5,
                 "seconds": 1.0},
                {"language": "zh", "voice": "Ryan", "chars": 10,
                 "seconds": 2.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        english = summary["languages"]["en"]
        self.assertEqual(english["n"], 2)
        self.assertAlmostEqual(english["chars_per_second"], 10.0)
        self.assertAlmostEqual(english["seconds_per_char"], 0.1)
        chinese = summary["languages"]["zh"]
        self.assertEqual(chinese["n"], 2)
        self.assertAlmostEqual(chinese["chars_per_second"], 5.0)
        self.assertAlmostEqual(chinese["seconds_per_char"], 0.2)
        self.assertAlmostEqual(
            summary["ratio_vs_en"]["by_voice"]["zh"]["Ryan"], 2.0)
        self.assertAlmostEqual(summary["ratio_vs_en"]["pooled"]["zh"], 2.0)
        self.assertNotIn("en", summary["ratio_vs_en"]["pooled"])

    def test_ratio_is_per_voice_and_pooled(self):
        rows = [{"language": "en", "voice": "Ryan", "chars": 6,
                 "seconds": 1.0},
                {"language": "en", "voice": "Vivian", "chars": 2,
                 "seconds": 1.0},
                {"language": "ja", "voice": "Ryan", "chars": 3,
                 "seconds": 1.0},
                {"language": "ja", "voice": "Vivian", "chars": 1,
                 "seconds": 1.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        self.assertAlmostEqual(
            summary["ratio_vs_en"]["by_voice"]["ja"]["Ryan"], 2.0)
        self.assertAlmostEqual(
            summary["ratio_vs_en"]["by_voice"]["ja"]["Vivian"], 2.0)
        # English pooled median 4 cps over Japanese pooled median 2 cps.
        self.assertAlmostEqual(summary["ratio_vs_en"]["pooled"]["ja"], 2.0)

    def test_no_ratio_without_english_rows(self):
        rows = [{"language": "zh", "voice": "Ryan", "chars": 5,
                 "seconds": 1.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        self.assertEqual(summary["ratio_vs_en"]["pooled"], {})
        self.assertEqual(summary["ratio_vs_en"]["by_voice"], {})

    def test_voice_missing_from_english_is_not_compared(self):
        rows = [{"language": "en", "voice": "Ryan", "chars": 6,
                 "seconds": 1.0},
                {"language": "ja", "voice": "Vivian", "chars": 1,
                 "seconds": 1.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        self.assertEqual(summary["ratio_vs_en"]["by_voice"]["ja"], {})
        self.assertAlmostEqual(summary["ratio_vs_en"]["pooled"]["ja"], 6.0)

    def test_rejects_non_positive_rows(self):
        rows = [{"language": "zh", "voice": "Ryan", "chars": 0,
                 "seconds": 1.0},
                {"language": "zh", "voice": "Ryan", "chars": 5,
                 "seconds": 0.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        self.assertEqual(summary["languages"], {})
        self.assertEqual(summary["ratio_vs_en"]["pooled"], {})

    def test_n_counts_only_usable_rows(self):
        rows = [{"language": "zh", "voice": "Ryan", "chars": 5,
                 "seconds": 1.0},
                {"language": "zh", "voice": "Ryan", "chars": 5,
                 "seconds": 0.0}]
        summary = cjk_speech_rate.get_rate_summary(rows)
        self.assertEqual(summary["languages"]["zh"]["n"], 1)


if __name__ == "__main__":
    unittest.main()


class AozoraFrameTests(unittest.TestCase):
    def test_title_line_and_colophon_are_dropped(self):
        text = "作者 題名\n\n\n　ある日の暮方の事である。雨が降る。\n\n底本：「全集」出版社\n入力：誰か\n"
        self.assertEqual("ある日の暮方の事である。雨が降る。", cjk_speech_rate.get_clean_text(text, "ja"))

