"""The classifier behind goal 5.5's record, on cases with known answers.

The target wants every term the plain reading fails to be EITHER an entry
measured to help OR recorded as one respelling could not fix. The trap is the
third state - a term nobody measured - which must not be quietly filed under
"could not fix", and the second trap is treating a lone rescue as settled when
a rescue reproduces only 26.6% of the time among terms measured twice.
"""
import os
import sys
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.lexicon_from_measurements import (  # noqa: E402
    classify, corroboration_rate)


def row(term, plain, respelled, arm="a.json", respelling="x-y"):
    return {"term": term, "plain_recovers_word": plain,
            "respelled_recovers_word": respelled, "respelling": respelling,
            "kana": "カナ", "books": 5, "_arm": arm}


class ClassifyTest(unittest.TestCase):
    def test_a_rescued_term_becomes_an_entry(self):
        entries, unfixable, fine = classify(
            {"alpha": [row("alpha", False, True)]})
        self.assertIn("alpha", entries)
        self.assertEqual([], unfixable)
        self.assertEqual("x-y", entries["alpha"]["respelling"])

    def test_a_term_no_respelling_saved_is_recorded_not_dropped(self):
        """The half of the target that is larger, and easy to lose."""
        entries, unfixable, fine = classify(
            {"beta": [row("beta", False, False)]})
        self.assertEqual({}, entries)
        self.assertEqual(1, len(unfixable))
        self.assertEqual("beta", unfixable[0]["term"])
        self.assertEqual(["a.json"], unfixable[0]["arms_tried"])

    def test_a_term_the_plain_reading_says_gets_no_entry(self):
        """Respelling breaks 69.7% of these. An entry here does harm."""
        entries, unfixable, fine = classify(
            {"gamma": [row("gamma", True, True)]})
        self.assertEqual({}, entries)
        self.assertEqual([], unfixable)
        self.assertEqual(["gamma"], fine)

    def test_rescue_in_one_arm_is_enough_to_qualify_but_is_marked(self):
        entries, _, _ = classify({"delta": [row("delta", False, True, "a.json"),
                                            row("delta", False, False, "b.json")]})
        self.assertIn("delta", entries)
        self.assertFalse(entries["delta"]["corroborated"])
        self.assertEqual(2, entries["delta"]["arms_measuring"])
        self.assertEqual(1, entries["delta"]["arms_rescuing"])

    def test_two_arms_agreeing_is_marked_corroborated(self):
        entries, _, _ = classify({"eps": [row("eps", False, True, "a.json"),
                                          row("eps", False, True, "b.json")]})
        self.assertTrue(entries["eps"]["corroborated"])

    def test_a_term_measured_once_is_not_counted_as_disagreement(self):
        """Most terms were measured by one arm only. Counting them as failures
        to reproduce would invent a disagreement that was never tested."""
        entries, _, _ = classify({"zeta": [row("zeta", False, True)]})
        rate = corroboration_rate(entries)
        self.assertEqual(0, rate["terms_measured_by_more_than_one_arm"])
        self.assertEqual(1, rate["terms_measured_once_only"])

    def test_the_agreement_rate_uses_only_terms_measured_twice(self):
        entries, _, _ = classify({
            "one": [row("one", False, True, "a.json")],                    # once
            "two": [row("two", False, True, "a.json"),
                    row("two", False, True, "b.json")],                    # agreed
            "three": [row("three", False, True, "a.json"),
                      row("three", False, False, "b.json")],               # split
        })
        rate = corroboration_rate(entries)
        self.assertEqual(2, rate["terms_measured_by_more_than_one_arm"])
        self.assertEqual(1, rate["rescued_by_more_than_one"])
        self.assertEqual(50.0, rate["agreement_pct"])
        self.assertEqual(1, rate["terms_measured_once_only"])


if __name__ == "__main__":
    unittest.main()
