"""The cast shown to the model must be able to express the right answer.

The two-stage arm scored 35-50% against a published 90.6%, and part of that was
the harness: the roster was built from the fixture's ALIAS groups, which cover
19 characters in Pride and Prejudice while its roster covers 74. Eight speakers
the gold expects appeared in no alias group at all - MR. BENNET, KITTY, MARY,
MR. HURST among them - together 167 of 1,270 lines. The model was handed a cast
that could not name the speaker for 13% of the book, and then scored wrong for
failing to.
"""
import glob
import json
import os
import unittest

from experiments.two_stage_attribution import roster_lines

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = sorted(glob.glob(os.path.join(
    REPO, "app", "fixtures", "attribution_gold_pdnc_*.json")))


class RosterCoverageTest(unittest.TestCase):
    @staticmethod
    def _names(lines):
        return {line.split(" (also")[0] for line in lines}

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_every_expected_speaker_can_be_named(self):
        """THE DEFECT: a question the model cannot answer correctly."""
        for path in FIXTURES:
            with self.subTest(book=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    fixture = json.load(fh)
                shown = self._names(roster_lines(fixture))
                expected = {e["expected_speaker"] for e in fixture["entries"]}
                self.assertEqual(set(), expected - shown)

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_aliases_are_shown_next_to_the_name(self):
        # Which spellings count as the same person is not obvious - this
        # corpus treats the bare surname BENNET as MRS. Bennet - and the
        # published formulation calls the alias list essential.
        with open(FIXTURES[0], encoding="utf-8") as fh:
            fixture = json.load(fh)
        lines = roster_lines(fixture)
        self.assertTrue(any("also called:" in line for line in lines))

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_no_character_is_listed_twice(self):
        for path in FIXTURES:
            with self.subTest(book=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    lines = roster_lines(json.load(fh))
                names = [line.split(" (also")[0] for line in lines]
                self.assertEqual(len(names), len(set(names)))

    def test_a_fixture_with_only_aliases_still_produces_a_cast(self):
        # Older fixtures carry no roster field; falling back to the alias
        # canonicals is right there, and only there.
        lines = roster_lines({"aliases": [["ELIZABETH", "LIZZY"], ["JANE"]]})
        self.assertEqual({"ELIZABETH", "JANE"}, self._names(lines))
