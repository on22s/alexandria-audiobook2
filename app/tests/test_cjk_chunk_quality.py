"""The #1332 chunk-quality instrument: packing, truncation and weighting."""
import unittest

from experiments.cjk_chunk_quality import (get_arm_summary, get_new_limit, get_packed_chunks,
                                           get_passage, is_truncated)


class PackingTests(unittest.TestCase):
    def test_limits_are_the_measured_factors(self):
        self.assertEqual(183, get_new_limit("zh"))
        self.assertEqual(239, get_new_limit("ja"))

    def test_packing_keeps_every_sentence_in_order_and_under_the_limit(self):
        sentences = ["甲" * 70 + "。", "乙" * 50 + "。", "丙" * 90 + "。", "丁" * 10 + "。"]
        chunks = get_packed_chunks(sentences, 183)
        self.assertEqual("".join(sentences), "".join(chunks))
        self.assertTrue(all(len(c) <= 183 for c in chunks))
        self.assertEqual(2, len(chunks))  # 71+51 fit; 91+11 fit

    def test_both_arms_cover_identical_text(self):
        sentences = [("字" * n) + "。" for n in (40, 120, 60, 150, 30, 90)]
        old, new = get_packed_chunks(sentences, 500), get_packed_chunks(sentences, 183)
        self.assertEqual("".join(old), "".join(new))
        self.assertLess(len(old), len(new))

    def test_a_sentence_over_the_limit_is_refused_not_split(self):
        with self.assertRaises(ValueError):
            get_packed_chunks(["字" * 200 + "。"], 183)

    def test_passage_skips_overlong_sentences_and_stops_at_target(self):
        sentences = ["短" * 9 + "。", "长" * 300 + "。", "中" * 49 + "。", "末" * 99 + "。"]
        passage = get_passage(sentences, 50, 183)
        self.assertEqual([sentences[0], sentences[2]], passage)


class ScoringTests(unittest.TestCase):
    def test_truncation_flags_only_clips_far_below_expected_length(self):
        self.assertFalse(is_truncated(seconds=45.0, chars=183, seconds_per_char=0.25))
        self.assertTrue(is_truncated(seconds=20.0, chars=183, seconds_per_char=0.25))

    def test_summary_weights_error_by_characters(self):
        clips = [{"chars": 400, "cer": 0.10, "truncated": False, "short_transcript": False},
                 {"chars": 100, "cer": 0.60, "truncated": True, "short_transcript": True}]
        summary = get_arm_summary(clips)
        self.assertAlmostEqual(0.20, summary["cer"])
        self.assertEqual(1, summary["truncated"])
        self.assertNotIn("reading_cer", summary)


if __name__ == "__main__":
    unittest.main()
