"""Scoring for the respelling measurement behind goal 5.5.

Every case below is a REAL transcript from the 5,880-term run, not an invented
one. The retired scorer counted each of the four failures as a perfect 1.0,
which is how respelling came to be reported as rescuing 38% of badly
pronounced words when it rescues 13%.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from experiments import measure_respellings as measure
from experiments import rescore_respellings as rescore


# (term, expected kana, transcript, should the word count as recovered)
REAL_CASES = [
    ("tanaka",  "タナカ",   "「タナカ」を言っていました。",                         True),
    ("chibi",   "チビ",    "「チビ」を前に行きました。",                          True),
    # A trailing long vowel does not break the word: シミズ is inside シミズウ.
    ("shimizu", "シミズ",   "「シミズウ」と言って、「シミズウ」を終わりに行く",             True),
    # The recognizer writes kanji where the candidate list holds katakana.
    ("ningen",  "ニンゲン",  "「スポーズ」と言って、人間の前に行きました。",               True),
    # --- the four that the old scorer called perfect ---
    ("futaba",  "フタバ",   "「フォータバー」を前に行く",                          False),
    ("seiichi", "セイイチ",  "「セイチー」を前に行く",                            False),
    ("saya",    "サヤ",    "「スーパーズ」と言って「サイヤー」がある",                  False),
    ("shizu",   "シズ",    "「シポーズン」と言って、シーブ・フューを見つけた",              False),
]


class RecoveryScoringTests(unittest.TestCase):
    def test_every_real_case_is_judged_correctly(self):
        for term, kana, heard, expected in REAL_CASES:
            with self.subTest(term=term):
                self.assertEqual(
                    expected, measure.score_recovery(kana, heard)["recovers_word"],
                    f"{term}: wanted {kana} in {heard!r}")

    def test_the_retired_scorer_would_have_failed_these(self):
        """Proves the cases are load-bearing rather than decorative.

        If the old scorer ever agreed with the new one on the four false
        positives, the regression this file guards would not exist and the
        fixtures would need replacing with ones that do discriminate.
        """
        fooled = [term for term, kana, heard, expected in REAL_CASES
                  if not expected
                  and measure.scattered_overlap(kana, heard) >= 1.0]
        self.assertEqual(["futaba", "seiichi", "saya", "shizu"], fooled)

    def test_a_word_absent_entirely_is_not_recovered(self):
        score = measure.score_recovery("タナカ", "「あなたは何を見せるのか?」")
        self.assertFalse(score["recovers_word"])
        self.assertLess(score["closeness"], 0.5)

    def test_kanji_and_katakana_are_compared_as_sounds(self):
        # 人間 and ニンゲン share no characters and are the same word.
        self.assertTrue(measure.score_recovery("ニンゲン", "人間です")["recovers_word"])

    def test_closeness_grades_a_near_miss_above_a_miss(self):
        near = measure.score_recovery("セイイチ", "「セイチー」を前に行く")["closeness"]
        miss = measure.score_recovery("セイイチ", "「あなたは何を見せるのか?」")["closeness"]
        self.assertGreater(near, miss)
        self.assertLess(near, 1.0)

    def test_missing_pykakasi_raises_instead_of_scoring_characters(self):
        # Falling back to character scoring would silently reintroduce the bug
        # while still reporting a "reading-scored" number.
        with patch.object(measure, "score_recovery", wraps=measure.score_recovery):
            with patch("experiments.asr_backends.to_reading", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "readings"):
                    measure.score_recovery("タナカ", "タナカ")

    def test_an_empty_expected_reading_is_not_a_free_pass(self):
        self.assertFalse(measure.score_recovery("", "なんでも")["recovers_word"])


class LongestCommonRunTests(unittest.TestCase):
    def test_the_whole_word_scores_one(self):
        self.assertEqual(1.0, measure.longest_common_run("たなか", "またなかを"))

    def test_scattered_characters_score_only_their_longest_run(self):
        # た...な...か spread across a sentence is 1/3, not 1.0 - this is the
        # exact difference between the two scorers.
        self.assertAlmostEqual(1 / 3, measure.longest_common_run("たなか", "たxxなxxか"))

    def test_nothing_in_common_scores_zero(self):
        self.assertEqual(0.0, measure.longest_common_run("たなか", "ほげ"))

    def test_an_empty_expectation_scores_zero_rather_than_one(self):
        self.assertEqual(0.0, measure.longest_common_run("", "なんでも"))


class RescoreTests(unittest.TestCase):
    def _row(self, plain_heard, respelled_heard):
        return {"term": "futaba", "kana": "フタバ", "respelling": "foo-tah-bah",
                "books": 9, "series": 3,
                "plain_heard": plain_heard, "respelled_heard": respelled_heard,
                "plain_kana_overlap": 0.0, "respelled_kana_overlap": 1.0,
                "delta": 1.0, "helps": True}

    def test_a_phantom_help_is_demoted_and_the_old_verdict_preserved(self):
        row = rescore.rescore_row(
            self._row("「あなたは何を見せるのか?」", "「フォータバー」を前に行く"))
        self.assertFalse(row["helps"], "the word never appeared")
        self.assertTrue(row["legacy_helps"], "the old verdict must survive for audit")
        self.assertEqual(1.0, row["legacy_respelled_scattered"])
        self.assertNotIn("respelled_kana_overlap", row)

    def test_a_genuine_help_survives_rescoring(self):
        row = rescore.rescore_row(
            self._row("「あなたは何を見せるのか?」", "「フタバ」を前に行く"))
        self.assertTrue(row["helps"])
        self.assertFalse(row["hurts"])

    def test_breaking_a_working_word_is_recorded_as_hurting(self):
        row = rescore.rescore_row(
            self._row("「フタバ」を前に行く", "「フォータバー」を前に行く"))
        self.assertTrue(row["hurts"])
        self.assertFalse(row["helps"])

    def test_a_skipped_row_passes_through_untouched(self):
        skipped = {"term": "sama", "kana": "サマ", "skipped": "unmappable kana"}
        self.assertEqual(skipped, rescore.rescore_row(skipped))

    def test_the_summary_counts_phantom_helps(self):
        rows = [
            rescore.rescore_row(self._row("何も", "「フォータバー」を前に行く")),
            rescore.rescore_row(self._row("何も", "「フタバ」を前に行く")),
        ]
        stats = rescore.summarize(rows)
        self.assertEqual(2, stats["scored"])
        self.assertEqual(1, stats["helps"])
        self.assertEqual(2, stats["legacy_helps"])
        self.assertEqual(1, stats["legacy_helps_where_word_never_appeared"])


if __name__ == "__main__":
    unittest.main()


class ArtifactCompletenessTest(unittest.TestCase):
    """A checkpointed artifact must say whether it finished.

    _write runs every five terms, so an interrupted run leaves a file that is
    indistinguishable from a finished one by inspection. On 2026-08-18 a
    70-minute cap killed the n1200 block at 1129 of 1200 terms; the artifact
    was committed as evidence and the chain's skip-if-exists would have
    treated it as done permanently.

    Truncation is not a smaller sample here, it is a biased one: terms are
    taken in book-count order, so what is missing is exactly the rarest words.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "artifact.json")

    def _write(self, done, requested):
        results = {f"term{i}": {"term": f"term{i}"} for i in range(done)}
        measure._write(self.path, results, [f"term{i}" for i in range(requested)])
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_finished_run_is_marked_complete(self):
        self.assertEqual("complete", self._write(10, 10)["status"])

    def test_an_interrupted_run_is_marked_partial(self):
        doc = self._write(1129, 1200)
        self.assertEqual("partial", doc["status"])
        # Both numbers stay readable: a reader should not need to trust the
        # label alone, and the label should not need the reader to do the sum.
        self.assertEqual(1200, doc["candidates_considered"])
        self.assertEqual(1129, len(doc["results"]))

    def test_the_checkpoint_partway_through_is_partial(self):
        # Every artifact is partial until the last checkpoint; that is exactly
        # why the field has to be written on every checkpoint, not at the end.
        self.assertEqual("partial", self._write(5, 400)["status"])
