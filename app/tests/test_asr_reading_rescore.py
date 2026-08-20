"""The per-reader reading rescorer, on cases whose answer is known.

The claim it overturns is strong - GOALS said every Japanese reader fails CER,
and this says every one passes - so the scorer gets fixtures where the right
answer is arithmetic, including the two ways it could produce a comfortable
wrong number: scoring characters while claiming to score readings, and
rewarding a hypothesis for matching an empty reference.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class ReaderIdTest(unittest.TestCase):
    def setUp(self):
        from experiments import asr_reading_rescore as m
        self.m = m

    def test_the_reader_is_everything_before_the_clip_number(self):
        self.assertEqual("botchan-by-soseki-natsume-2",
                         self.m.reader_of("botchan-by-soseki-natsume-2-00001"))

    def test_a_reader_name_ending_in_a_digit_is_not_truncated(self):
        """'botchan-by-soseki-natsume-2' ends in a digit and is followed by a
        digit block. A greedy split would file every one of its clips under
        'botchan-by-soseki-natsume', merging two readers into one and hiding a
        failing reader inside a passing one."""
        self.assertEqual("botchan-by-soseki-natsume-2",
                         self.m.reader_of("botchan-by-soseki-natsume-2-00013"))

    def test_an_id_with_no_index_has_no_reader(self):
        self.assertIsNone(self.m.reader_of("nothing-here"))
        self.assertIsNone(self.m.reader_of(None))


class ReadingScoreTest(unittest.TestCase):
    """The real pair from the artifact: identical words, different script."""

    def setUp(self):
        from asr_backends import to_reading, word_error_rate
        self.to_reading = to_reading
        self.wer = word_error_rate
        if to_reading("私") is None:
            self.skipTest("pykakasi unavailable")

    def test_kanji_and_kana_spellings_of_one_word_score_as_equal(self):
        """私 and わたし are the same word read aloud. Character scoring calls
        them completely different, which is the whole reason this measure
        exists."""
        self.assertEqual(self.to_reading("私"), self.to_reading("わたし"))

    def test_the_real_botchan_pair_scores_far_better_on_readings(self):
        ref = "おやゆずりのむてっぽうで小供の時から損ばかりしている。"
        hyp = "親譲りの持てっぽうで 子供の時から損ばかりしている"
        written = self.wer(ref, hyp, char_level=True)
        reading = self.wer(self.to_reading(ref), self.to_reading(hyp),
                           char_level=True)
        self.assertLess(reading, written,
                        "the pair that motivated the whole correction must "
                        "score better on readings than on characters")

    def test_a_genuine_mishearing_is_still_punished(self):
        """The measure must not simply flatter everything. Different words with
        different readings have to stay wrong, or a 9.9% would mean nothing."""
        a, b = self.to_reading("犬が走る"), self.to_reading("猫が眠る")
        self.assertGreater(self.wer(a, b, char_level=True), 0.3)

    def test_conversion_drops_punctuation_not_content(self):
        self.assertEqual(self.to_reading("こんにちは。"), self.to_reading("こんにちは"))


class AggregationTest(unittest.TestCase):
    def setUp(self):
        from experiments import asr_reading_rescore as m
        self.m = m

    def test_a_failing_reader_is_named_not_absorbed_by_the_pool(self):
        """A pool can pass while a member fails, and the correction's whole
        claim is per-reader. If this ever stops working the document would
        report 'every reader passes' off a pooled mean again."""
        rows = ([{"id": "good-1", "reader": "good", "cer_reading": 0.05,
                  "cer_written": 0.3}] * 40 +
                [{"id": "bad-1", "reader": "bad", "cer_reading": 0.55,
                  "cer_written": 0.6}] * 2)
        summary = self.m.summarise(rows)
        self.assertTrue(summary["good"]["passes_target"])
        self.assertFalse(summary["bad"]["passes_target"])

    def test_the_target_is_the_one_goals_states(self):
        self.assertEqual(0.20, self.m.TARGET_CER)

    def test_hypotheses_are_read_from_every_backend_present(self):
        payload = {"results": {
            "silero_whisper_cpp": {"hypotheses": [
                {"id": "a-1", "reference": "あ", "hypothesis": "あ", "wer": 0.0}]},
            "other": {"hypotheses": [
                {"id": "b-1", "reference": "い", "hypothesis": "い", "wer": 0.0}]}}}
        got = self.m.collect_hypotheses(payload)
        self.assertEqual(2, len(got))
        self.assertEqual({"silero_whisper_cpp", "other"}, {g[3] for g in got})

    def test_an_artifact_without_hypotheses_yields_nothing(self):
        """The four reader artifacts are exactly this shape - aggregates only.
        Returning [] is what makes the caller refuse instead of reporting a
        score over zero clips."""
        self.assertEqual([], self.m.collect_hypotheses(
            {"results": {"silero_whisper_cpp": {"n": 13, "wer_mean": 0.34}}}))


if __name__ == "__main__":
    unittest.main()
