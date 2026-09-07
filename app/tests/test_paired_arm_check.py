"""The pairing-integrity check, on cases whose answer is known.

Rule 21. The point of this instrument is to say whether a caveat MATTERS, so it
has to be able to answer both ways: it must find a real break, and it must come
back empty on a sound comparison. A checker that always finds something would
have "confirmed" the mushoku16 caveat instead of retiring it.
"""

import unittest

from experiments.paired_arm_check import (arm_rows, check, compare,
                                          prompt_disagreements)


def rows(spec, arm, prompt="p"):
    """spec: {id: correct}. Every row carries the same prompt hash by default."""
    return [{"id": i, "arm": arm, "correct": c,
             "prompt_sha256": prompt(i) if callable(prompt) else prompt}
            for i, c in spec.items()]


def doc(book, base_spec, tuned_spec, base_prompt="p", tuned_prompt="p"):
    return {"meta": {"gold_files": {book: "sha"}},
            "rows": (rows(base_spec, "base", base_prompt)
                     + rows(tuned_spec, "tuned", tuned_prompt))}


class PromptDisagreementTest(unittest.TestCase):

    def test_a_sound_comparison_reports_nothing(self):
        # THE CASE IT MUST REJECT, and the one that mattered in practice.
        d = doc("b", {1: True, 2: False}, {1: True, 2: True})
        bad, unknown = prompt_disagreements(arm_rows(d, "base"),
                                            arm_rows(d, "tuned"))
        self.assertEqual([], bad)
        self.assertEqual([], unknown)

    def test_it_finds_a_row_whose_arms_saw_different_prompts(self):
        d = doc("b", {1: True, 2: False}, {1: True, 2: True},
                tuned_prompt=lambda i: "retry" if i == 2 else "p")
        bad, unknown = prompt_disagreements(arm_rows(d, "base"),
                                            arm_rows(d, "tuned"))
        self.assertEqual([2], bad)
        self.assertEqual([], unknown)

    def test_a_missing_hash_is_unknown_and_never_agreement(self):
        # NULL == NULL reporting "same prompt" is the bug that made
        # prompt_sha256 useless for months; inheriting it here would make this
        # checker worse than not having one.
        d = doc("b", {1: True}, {1: False})
        for row in d["rows"]:
            row["prompt_sha256"] = None
        bad, unknown = prompt_disagreements(arm_rows(d, "base"),
                                            arm_rows(d, "tuned"))
        self.assertEqual([], bad)
        self.assertEqual([1], unknown)


class CompareTest(unittest.TestCase):

    def test_it_counts_repairs_and_breaks_not_just_totals(self):
        # Equal accuracy can hide a lot of churn, and the paired test uses the
        # churn, not the totals.
        d = doc("b", {1: True, 2: False}, {1: False, 2: True})
        result = compare(arm_rows(d, "base"), arm_rows(d, "tuned"))
        self.assertEqual(50.0, result["base"])
        self.assertEqual(50.0, result["tuned"])
        self.assertEqual(1, result["repaired"])
        self.assertEqual(1, result["broken"])

    def test_no_discordant_pairs_is_p_one(self):
        d = doc("b", {1: True, 2: False}, {1: True, 2: False})
        self.assertEqual(1.0, compare(arm_rows(d, "base"),
                                      arm_rows(d, "tuned"))["p"])


class CheckTest(unittest.TestCase):

    def test_it_can_show_a_caveat_does_not_matter(self):
        # The shape of the real 2026-09-07 case: one broken pair among many
        # sound ones, and the verdict is unchanged without it.
        base = {i: False for i in range(20)}
        tuned = {i: True for i in range(20)}
        tuned[19] = False
        report = check([doc("b", base, tuned,
                            tuned_prompt=lambda i: "retry" if i == 0 else "p")])
        self.assertEqual([("b", 0)], report["mismatched_prompts"])
        self.assertLess(report["pooled"]["p"], 0.05)
        self.assertLess(report["pooled_matched_only"]["p"], 0.05)
        self.assertEqual(19, report["pooled_matched_only"]["n"])

    def test_it_can_show_a_caveat_DOES_matter(self):
        # And the other way, or it cannot discriminate. Here every repair comes
        # from rows whose pairing is broken, so removing them removes the
        # finding - which is the case this check exists to catch.
        base = {i: False for i in range(10)}
        tuned = {i: i < 6 for i in range(10)}
        report = check([doc("b", base, tuned,
                            tuned_prompt=lambda i: "retry" if i < 6 else "p")])
        self.assertEqual(6, len(report["mismatched_prompts"]))
        self.assertEqual(6, report["pooled"]["repaired"])
        self.assertEqual(0, report["pooled_matched_only"]["repaired"])
        self.assertEqual(1.0, report["pooled_matched_only"]["p"])

    def test_pooling_two_books_keeps_their_ids_apart(self):
        # Two books using the same row ids must not collide into one another;
        # that would silently drop half the rows.
        report = check([doc("one", {1: True}, {1: True}),
                        doc("two", {1: False}, {1: True})])
        self.assertEqual(2, report["pooled"]["n"])
        self.assertEqual({"one", "two"}, set(report["books"]))


if __name__ == "__main__":
    unittest.main()
