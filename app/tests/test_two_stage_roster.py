"""The two-stage arm must measure the method, not the harness.

This experiment produced four wrong results before a review, and the review
found five more ways it could produce a plausible number that was not a
measurement. Each test below pins one of them.
"""
import glob
import json
import os
import unittest

from experiments.two_stage_attribution import (clean_answer, is_decline,
                                               roster_lines, summarise)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = sorted(glob.glob(os.path.join(
    REPO, "app", "fixtures", "attribution_gold_pdnc_*.json")))


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class RosterTest(unittest.TestCase):
    @staticmethod
    def _names(lines):
        return {line.split(" [also")[0] for line in lines}

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_every_expected_speaker_can_be_named(self):
        for path in FIXTURES:
            with self.subTest(book=os.path.basename(path)):
                fixture = _load(path)
                shown = self._names(roster_lines(fixture))
                expected = {e["expected_speaker"] for e in fixture["entries"]}
                self.assertEqual(set(), expected - shown)

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_aliases_attach_to_the_name_the_gold_uses(self):
        """PDNC alias groups are alphabetical, so group[0] is not canonical.

        Taking it decorated ELIZA and left ELIZABETH - 401 gold lines - bare.
        """
        fixture = _load(FIXTURES[0])
        lines = {l.split(" [also")[0]: l for l in roster_lines(fixture)}
        speakers = [e["expected_speaker"] for e in fixture["entries"]]
        busiest = max(set(speakers), key=speakers.count)
        group = next((g for g in fixture.get("aliases") or []
                      if busiest in g), None)
        if group and len(group) > 1:
            self.assertIn("also:", lines[busiest])

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_the_cast_is_not_inflated_with_duplicates(self):
        """84 lines for a 74-name book meant ten characters listed twice."""
        for path in FIXTURES:
            with self.subTest(book=os.path.basename(path)):
                fixture = _load(path)
                lines = roster_lines(fixture)
                self.assertEqual(len(lines), len(self._names(lines)))
                roster = fixture.get("roster")
                if roster:
                    self.assertLessEqual(len(lines), len(roster) + 1)

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_no_decorated_name_is_a_character_who_never_speaks(self):
        fixture = _load(FIXTURES[0])
        speakers = {e["expected_speaker"] for e in fixture["entries"]}
        phantom = [l for l in roster_lines(fixture)
                   if "also:" in l and l.split(" [also")[0] not in speakers]
        self.assertEqual([], phantom)


class AnswerShapeTest(unittest.TestCase):
    """Replies arrive decorated; an exact comparison scored them all wrong."""

    def test_markdown_quotes_and_punctuation_are_stripped(self):
        for reply in ("**ELIZABETH**", '"ELIZABETH"', "ELIZABETH.",
                      "`ELIZABETH`", "  ELIZABETH  "):
            self.assertEqual("ELIZABETH", clean_answer(reply), reply)

    def test_an_echoed_cast_line_resolves_to_the_name(self):
        # The prompt shows "NAME [also: ...]"; a model that echoes the line is
        # obeying, and used to be scored wrong for it.
        self.assertEqual("ELIZABETH",
                         clean_answer("ELIZABETH [also: ELIZA, LIZZY]"))

    def test_a_decline_is_recognised_however_it_is_decorated(self):
        for reply in ("UNKNOWN", "**UNKNOWN**", '"UNKNOWN"',
                      "UNKNOWN - I cannot tell"):
            self.assertTrue(is_decline(clean_answer(reply)), reply)

    def test_a_name_is_not_mistaken_for_a_decline(self):
        self.assertFalse(is_decline(clean_answer("MR. UNKNOWNSON")))


class SummaryTest(unittest.TestCase):
    """Failures, declines and wrong answers are three different things."""

    def _rows(self, predicted, correct=False, kind="Explicit"):
        return [{"predicted": p, "correct": c,
                 "candidate_provenance": f"single|book|{kind}"}
                for p, c in zip(predicted, correct or [False] * len(predicted))]

    def test_failed_requests_have_no_accuracy(self):
        out = summarise(self._rows([None, None]))
        self.assertEqual(2, out["failed_requests"])
        self.assertIsNone(out["accuracy_when_answered"])

    def test_accuracy_is_over_what_was_answered(self):
        out = summarise(self._rows(["A", "B", None], [True, False, False]))
        self.assertEqual(1, out["failed_requests"])
        self.assertEqual(0.5, out["accuracy_when_answered"])

    def test_a_decline_is_not_a_wrong_answer(self):
        out = summarise(self._rows(["A", "UNKNOWN"], [True, False]))
        self.assertEqual(1, out["declined"])
        self.assertEqual(1.0, out["accuracy_when_answered"])
        # Charged as a miss only in the figure that says it does.
        self.assertEqual(0.5, out["accuracy_counting_declines"])

    def test_every_quote_type_bucket_carries_every_category(self):
        """Per-type rates were uncomputable: no 'wrong', and failures folded
        into n."""
        out = summarise(self._rows(["A", "UNKNOWN", None, "B"],
                                   [True, False, False, False]))
        bucket = out["by_quote_type"]["Explicit"]
        self.assertEqual({"n", "correct", "declined", "failed", "wrong"},
                         set(bucket))
        self.assertEqual(4, bucket["n"])
        self.assertEqual(1, bucket["wrong"])
        self.assertEqual(1, bucket["failed"])


class CandidateRecordingTest(unittest.TestCase):
    """`in_candidates` must mean what it says.

    two_stage_attribution passed the roster DISPLAY lines - "MRS. BENNET
    [also: BENNET]" - to ExperimentRecord.add, whose in_candidates is an exact
    membership test. The artifact therefore reported the expected speaker
    missing from the cast on 2,250 of 2,494 rows. The true figure is zero: the
    model was handed the right name every single time, and the failure is
    entirely one of selection.

    That inversion is why this is a crash now and not a lenient parse. A field
    that is silently wrong gets analysed; one that raises does not.
    """

    def test_roster_names_expands_aliases(self):
        from experiments.two_stage_attribution import roster_names
        self.assertEqual(
            ["MRS. BENNET", "BENNET", "MR. DARCY", "JANE", "MISS BENNET"],
            roster_names(["MRS. BENNET [also: BENNET]", "MR. DARCY",
                          "JANE [also: MISS BENNET]"]))

    def test_a_name_with_no_aliases_survives_unchanged(self):
        from experiments.two_stage_attribution import roster_names
        self.assertEqual(["MR. DARCY"], roster_names(["MR. DARCY"]))

    @unittest.skipUnless(FIXTURES, "no PDNC fixtures present")
    def test_every_gold_speaker_is_in_the_recorded_candidate_set(self):
        """The property the broken field denied, on every real fixture.

        The artifact claimed the expected speaker was absent from the cast on
        2,250 of 2,494 rows. This asserts the opposite directly against the
        gold: for each book, every speaker the gold names appears in the
        candidate set the artifact would record.
        """
        from experiments.two_stage_attribution import roster_lines, roster_names
        for path in FIXTURES:
            with self.subTest(book=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    fixture = json.load(handle)
                names = set(roster_names(roster_lines(fixture)))
                missing = sorted({e["expected_speaker"]
                                  for e in fixture["entries"]} - names)
                self.assertEqual([], missing,
                                 "gold speakers absent from the recorded cast")

    def test_the_manifest_refuses_display_lines(self):
        from experiments.manifest import _checked_candidates
        with self.assertRaises(ValueError):
            _checked_candidates(["MRS. BENNET [also: BENNET]"])
        self.assertEqual(["MRS. BENNET"], _checked_candidates(["MRS. BENNET"]))
