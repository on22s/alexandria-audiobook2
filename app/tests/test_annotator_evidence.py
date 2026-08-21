"""Joining our rows to PDNC's own evidence column, and not overstating the match.

The measured answer is that having the annotator's referring expression inside
our context window is worth +8.53 points on Explicit quotes, +2.45 on Implicit,
and -1.65 on Anaphoric - where the expression is a pronoun and identifies
nobody without resolution. Small everywhere. That is the point: the model has
the evidence and is largely not using it.

The match is EXACT substring after folding to lowercase alphanumerics, chosen
so the error runs one way only. A referring expression present in a different
surface form is counted absent, which understates the benefit of presence and
overstates how many rows lack evidence. A looser matcher would flatter the
result, so the strictness is pinned here.
"""
import unittest

from experiments.annotator_evidence import classify, match_book, normalise


def entry(prev="", line="", nxt="", quote_type="Explicit"):
    return {"prev_context": prev, "line": line, "next_context": nxt,
            "quote_type": quote_type}


class NormaliseTest(unittest.TestCase):
    def test_case_and_punctuation_fold_away(self):
        self.assertEqual("said his lady to him",
                         normalise('  "Said HIS lady, to him!"  '))

    def test_nothing_is_empty(self):
        self.assertEqual("", normalise(None))
        self.assertEqual("", normalise("   ,,,   "))


class MatchBookTest(unittest.TestCase):
    """Fixture stems are lowercase and unpunctuated; PDNC folders are not."""

    def test_a_fixture_stem_finds_its_pdnc_folder(self):
        raw = {"PrideAndPrejudice": {}, "TheSignOfTheFour": {}}
        self.assertEqual("PrideAndPrejudice", match_book("prideandprejudice", raw))
        self.assertEqual("TheSignOfTheFour", match_book("thesignofthefour", raw))

    def test_an_unknown_stem_is_none_rather_than_a_wrong_book(self):
        self.assertIsNone(match_book("mushoku16", {"PrideAndPrejudice": {}}))


class ClassifyTest(unittest.TestCase):
    def test_the_expression_is_found_across_the_whole_window(self):
        row = {"referringExpression": "said his lady to him"}
        self.assertTrue(classify(row, entry(nxt='," said his lady to him.')))
        self.assertTrue(classify(row, entry(prev="said his lady to him, and")))

    def test_punctuation_between_the_two_does_not_defeat_the_match(self):
        row = {"referringExpression": "cried Elizabeth"}
        self.assertTrue(classify(row, entry(nxt='"  --  cried Elizabeth!')))

    def test_a_different_surface_form_counts_as_absent(self):
        """Deliberate: the error must understate presence, never overstate it."""
        row = {"referringExpression": "said Mrs. Bennet"}
        self.assertFalse(classify(row, entry(nxt='" said his lady.')))

    def test_an_expression_nowhere_in_the_window_is_outside(self):
        row = {"referringExpression": "said the housekeeper"}
        self.assertFalse(classify(row, entry(prev="The hall was cold.",
                                             nxt="She turned away.")))

    def test_no_referring_expression_is_none_not_false(self):
        """None means unanswerable, False means answerable and absent."""
        self.assertIsNone(classify({"referringExpression": ""}, entry()))
        self.assertIsNone(classify({}, entry()))

    def test_the_line_itself_is_part_of_the_window(self):
        row = {"referringExpression": "asked Watson"}
        self.assertTrue(classify(row, entry(line="asked Watson")))
