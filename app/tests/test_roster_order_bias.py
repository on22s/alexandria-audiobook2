"""The cast list's order leaks into the answer, and the arm that tests removing it.

Measured on 2,494 stored rows: when the model is wrong, its answer sits EARLIER
in the alphabetical cast list than the correct one 563 times against 275 - 67.2%,
sign test p = 1.3e-23. Paired against each row's own gold, so cast composition
cancels and a coin would say 50%.

The unpaired figures (gold 0.405, predicted 0.359) are NOT the finding and must
not be read as one: alphabetical order is not random with respect to who speaks.
That is why `analyse` reports them under a key that says so.
"""
import unittest

from experiments.roster_order_bias import analyse, position, sign_test
from experiments.two_stage_attribution import PROMPT_VARIANTS, build_prompt

NAMES = ["ALICE", "BOB", "CARL", "DORA", "EVE"]


def fixture(entries, roster=None, aliases=None):
    return {"roster": roster or NAMES, "aliases": aliases or [],
            "entries": entries}


def entry(eid, expected):
    return {"id": eid, "expected_speaker": expected, "line": "a line"}


def row(eid, expected, predicted, correct):
    return {"id": "bk:" + eid, "expected": expected,
            "predicted": predicted, "correct": correct}


class PositionTest(unittest.TestCase):
    def test_first_and_last_are_zero_and_one(self):
        self.assertEqual(0.0, position("ALICE", NAMES, []))
        self.assertEqual(1.0, position("EVE", NAMES, []))

    def test_an_alias_finds_its_roster_entry(self):
        self.assertEqual(0.0, position("LIZZY", ["ELIZABETH", "BOB"],
                                       [{"ELIZABETH", "LIZZY"}]))

    def test_a_name_not_in_the_roster_is_none(self):
        self.assertIsNone(position("ZARA", NAMES, []))

    def test_a_single_name_roster_does_not_divide_by_zero(self):
        self.assertEqual(0.0, position("ALICE", ["ALICE"], []))


class SignTest(unittest.TestCase):
    def test_a_lopsided_split_is_significant(self):
        self.assertLess(sign_test(563, 275), 1e-20)

    def test_an_even_split_is_not(self):
        self.assertGreater(sign_test(50, 50), 0.5)

    def test_no_observations_is_one(self):
        self.assertEqual(1.0, sign_test(0, 0))


class AnalyseTest(unittest.TestCase):
    def _run(self, rows, fixture_entries):
        return analyse(rows, {"bk": fixture(fixture_entries)})

    def test_only_wrong_rows_enter_the_paired_count(self):
        """A correct row has nothing to say about which way the bias runs."""
        out = self._run(
            [row("q1", "EVE", "ALICE", False), row("q2", "BOB", "BOB", True)],
            [entry("q1", "EVE"), entry("q2", "BOB")])
        paired = out["paired_on_wrong_rows"]
        self.assertEqual(1, paired["wrong_answer_earlier_than_gold"])
        self.assertEqual(0, paired["wrong_answer_later_than_gold"])
        self.assertEqual(2, out["rows_positioned"])

    def test_a_wrong_answer_later_than_gold_counts_the_other_way(self):
        out = self._run([row("q1", "ALICE", "EVE", False)], [entry("q1", "ALICE")])
        self.assertEqual(1, out["paired_on_wrong_rows"]["wrong_answer_later_than_gold"])

    def test_a_row_whose_answer_is_off_roster_is_skipped(self):
        out = self._run([row("q1", "EVE", "ZARA", False)], [entry("q1", "EVE")])
        self.assertEqual(0, out["rows_positioned"])

    def test_the_unpaired_figures_carry_their_caveat(self):
        out = self._run([row("q1", "EVE", "ALICE", False)], [entry("q1", "EVE")])
        self.assertIn("not random", out["unpaired"]["note"])


class ShuffledRosterVariantTest(unittest.TestCase):
    ENTRY = {"id": "Q7", "line": "x", "prev_context": "a", "next_context": "b"}

    @staticmethod
    def _order(text):
        return [line[2:] for line in text.splitlines() if line.startswith("- ")]

    def test_the_variant_is_offered(self):
        self.assertIn("shuffled_roster", PROMPT_VARIANTS)

    def test_it_reorders_without_adding_or_dropping_a_name(self):
        control = self._order(build_prompt(self.ENTRY, NAMES))
        shuffled = self._order(build_prompt(self.ENTRY, NAMES,
                                            variant="shuffled_roster"))
        self.assertNotEqual(control, shuffled)
        self.assertEqual(sorted(control), sorted(shuffled))

    def test_the_same_row_always_gets_the_same_order(self):
        """Seeded by the row, not the run, so an arm is reproducible."""
        a = build_prompt(self.ENTRY, NAMES, variant="shuffled_roster")
        b = build_prompt(self.ENTRY, NAMES, variant="shuffled_roster")
        self.assertEqual(a, b)

    def test_different_rows_get_different_orders(self):
        """One fixed reshuffle would swap one systematic order for another."""
        other = dict(self.ENTRY, id="Q8")
        self.assertNotEqual(
            self._order(build_prompt(self.ENTRY, NAMES, variant="shuffled_roster")),
            self._order(build_prompt(other, NAMES, variant="shuffled_roster")))

    def test_the_caller_s_roster_is_not_mutated(self):
        names = list(NAMES)
        build_prompt(self.ENTRY, names, variant="shuffled_roster")
        self.assertEqual(NAMES, names)

    def test_an_unknown_variant_is_still_refused(self):
        with self.assertRaises(ValueError):
            build_prompt(self.ENTRY, NAMES, variant="shuffled_rostr")
