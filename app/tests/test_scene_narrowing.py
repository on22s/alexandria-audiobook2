"""Ranking the cast by recency, and the substring bug that inflated it.

The first version of this measurement matched names without word boundaries.
MARIA matched inside "Marianne" and ANNE inside "Anneliese", so characters were
scored as present who were never mentioned, and the window looked kinder than
it is. Corrected, on the same 2,494 rows:

    K=10 coverage    .877 -> .807
    rows lost         152 -> 268
    best case      -26 rows -> -121 rows (-4.85 points)

The verdict flipped from "roughly break-even" to "decisively negative", which
is the whole output of the experiment. A test that only checked the happy path
would have passed on both. `test_a_name_inside_a_longer_name_is_not_a_mention`
is the one that fails on the old form.
"""
import unittest

from experiments.scene_narrowing import analyse, recent_mentions, surface_forms

ROSTER = ["ELIZABETH", "MARIA", "MR. DARCY", "JANE", "ANNE"]
GROUPS = [{"ELIZABETH", "LIZZY"}]


class RecentMentionTest(unittest.TestCase):
    def test_nearest_the_end_comes_first(self):
        text = "Jane spoke first. Then Maria answered. Elizabeth said nothing."
        self.assertEqual(["ELIZABETH", "MARIA", "JANE"],
                         recent_mentions(text, ROSTER, GROUPS))

    def test_a_name_inside_a_longer_name_is_not_a_mention(self):
        """The bug: MARIA inside Marianne, ANNE inside Anneliese."""
        text = "Marianne and Anneliese walked on."
        self.assertEqual([], recent_mentions(text, ROSTER, GROUPS))

    def test_an_alias_counts_as_its_roster_name(self):
        self.assertEqual(["ELIZABETH"], recent_mentions("Lizzy laughed.",
                                                        ROSTER, GROUPS))

    def test_the_last_mention_wins_not_the_first(self):
        """A character named early and again late is NEAR, not far."""
        text = "Jane entered. " + ("filler " * 20) + "Jane turned. Maria left."
        self.assertEqual(["MARIA", "JANE"], recent_mentions(text, ROSTER, GROUPS))

    def test_empty_text_mentions_nobody(self):
        self.assertEqual([], recent_mentions("", ROSTER, GROUPS))
        self.assertEqual([], recent_mentions(None, ROSTER, GROUPS))

    def test_surface_forms_pull_in_the_whole_alias_group(self):
        self.assertEqual({"ELIZABETH", "LIZZY"},
                         surface_forms("Elizabeth", GROUPS))


class BucketTest(unittest.TestCase):
    """Each bucket is a different decision about narrowing; none may merge."""

    def _run(self, expected, predicted, correct, prev, window=2):
        gold = {("bk", "q1"): {"id": "q1", "prev_context": prev}}
        rows = [{"id": "bk:q1", "expected": expected,
                 "predicted": predicted, "correct": correct}]
        _, buckets, scored, _, _ = analyse(
            rows, gold, {"bk": ROSTER}, {"bk": GROUPS}, window, (1,))
        self.assertEqual(1, scored)
        return {k: v for k, v in buckets.items() if v}

    def test_gained_is_wrong_now_with_the_answer_inside_the_window(self):
        self.assertEqual({"gained": 1}, self._run(
            "JANE", "MR. DARCY", False, "Maria spoke. Jane spoke."))

    def test_lost_is_correct_now_with_the_gold_outside_the_window(self):
        self.assertEqual({"lost": 1}, self._run(
            "MR. DARCY", "MR. DARCY", True, "Maria spoke. Jane spoke."))

    def test_local_is_wrong_with_both_inside_the_window(self):
        self.assertEqual({"local": 1}, self._run(
            "JANE", "MARIA", False, "Maria spoke. Jane spoke."))

    def test_absent_is_wrong_with_the_gold_nowhere_near(self):
        self.assertEqual({"absent": 1}, self._run(
            "MR. DARCY", "JANE", False, "Maria spoke. Jane spoke."))

    def test_a_row_with_no_gold_entry_is_skipped_not_counted_wrong(self):
        rows = [{"id": "bk:missing", "expected": "JANE",
                 "predicted": "JANE", "correct": True}]
        _, buckets, scored, _, _ = analyse(rows, {}, {}, {}, 2, (1,))
        self.assertEqual(0, scored)
        self.assertEqual(0, sum(buckets.values()))
