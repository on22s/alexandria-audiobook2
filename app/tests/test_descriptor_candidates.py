"""Role labels as candidates, and the confound they turned out not to be.

PDNC rosters carry `A BLUFF, GENIAL INSPECTOR`, `A HINDOO SERVANT`, `A MAID`,
`THE COLONEL`. Those begin with "A" and sort to the TOP of an alphabetical
roster - so if the model over-picked them, #383's finding that wrong answers
sit early in the list would be a preference for a KIND of candidate wearing
the costume of a preference for POSITION.

Measured: descriptors are 10.5% of roster entries but only 1.4% of gold
answers, and 18 of 838 wrong rows reach for one. Too few to move a 67.2%
effect, so #383 stands.

The detector is the load-bearing part and is deliberately generous - an article,
an embedded description, or a bare occupational noun. Generosity is safe here
because a LARGER descriptor count would make the confound look worse, not
better; the finding is that even counted broadly they are too rare.
"""
import unittest

from experiments.descriptor_candidates import analyse, is_descriptor


class DetectorTest(unittest.TestCase):
    def test_articles_mark_a_description(self):
        for name in ("A MAID", "AN OLD WOMAN", "THE COLONEL",
                     "A WEARY-LOOKING POLICE SERGEANT"):
            self.assertTrue(is_descriptor(name), name)

    def test_an_embedded_description_is_caught_by_its_comma(self):
        self.assertTrue(is_descriptor("A BLUFF, GENIAL INSPECTOR"))
        self.assertTrue(is_descriptor("A SMALL, DARK, BRISK MAN"))

    def test_bare_occupational_nouns_count(self):
        for name in ("BUTLER", "MAID", "SERVANT", "NARRATOR", "CROWD"):
            self.assertTrue(is_descriptor(name), name)

    def test_real_names_do_not(self):
        for name in ("MR. DARCY", "ELIZABETH", "ABDULLAH KHAN",
                     "COLONEL FITZWILLIAM", "DR. WATSON"):
            self.assertFalse(is_descriptor(name), name)

    def test_a_name_beginning_with_a_is_not_an_article(self):
        """ABDULLAH must not read as `A` + description."""
        self.assertFalse(is_descriptor("ABDULLAH KHAN"))
        self.assertFalse(is_descriptor("ANNE DE BOURGH"))

    def test_blank_input_is_not_a_descriptor(self):
        self.assertFalse(is_descriptor(""))
        self.assertFalse(is_descriptor(None))


def fixture(roster, entries):
    return {"roster": roster, "aliases": [], "entries": entries}


def entry(eid, expected):
    return {"id": eid, "expected_speaker": expected, "line": "x"}


def row(eid, expected, predicted, correct):
    return {"id": "bk:" + eid, "expected": expected,
            "predicted": predicted, "correct": correct}


class AnalyseTest(unittest.TestCase):
    ROSTER = ["A MAID", "ELIZABETH", "MR. DARCY"]

    def _run(self, rows, entries):
        return analyse(rows, {"bk": fixture(self.ROSTER, entries)})

    def test_gold_kind_splits_the_rows(self):
        out = self._run([row("q1", "A MAID", "A MAID", True),
                         row("q2", "ELIZABETH", "MR. DARCY", False)],
                        [entry("q1", "A MAID"), entry("q2", "ELIZABETH")])
        self.assertEqual(1, out["by_gold_kind"]["descriptor"]["n"])
        self.assertEqual(1, out["by_gold_kind"]["named"]["n"])

    def test_reaching_for_a_descriptor_is_counted_separately(self):
        """The confound, if it existed, would live in this cell."""
        out = self._run([row("q1", "ELIZABETH", "A MAID", False)],
                        [entry("q1", "ELIZABETH")])
        self.assertEqual(1, out["wrong_rows_by_kind"]["named -> descriptor"])

    def test_correct_rows_never_enter_the_wrong_row_counts(self):
        out = self._run([row("q1", "ELIZABETH", "ELIZABETH", True)],
                        [entry("q1", "ELIZABETH")])
        self.assertEqual({}, out["wrong_rows_by_kind"])

    def test_the_roster_share_is_reported_separately_from_the_gold_share(self):
        """10.5% of candidates against 1.4% of answers - they are mostly noise."""
        out = self._run([row("q1", "ELIZABETH", "ELIZABETH", True)],
                        [entry("q1", "ELIZABETH")])
        self.assertAlmostEqual(1 / 3, out["mean_roster_share_descriptors"], 3)

    def test_a_row_whose_gold_is_off_roster_is_skipped(self):
        out = self._run([row("q1", "ZARA", "ELIZABETH", False)],
                        [entry("q1", "ZARA")])
        self.assertEqual(0, out["rows"])
