"""Reading a shipped script's dialogue map, and not overstating what it shows.

Without gold this measures DISAGREEMENT with the pipeline, never accuracy, and
the two must not be conflated in the artifact or in the code. The parsing is
where it can go quietly wrong: `spoken` and `source_span` survive a JSON round
trip as the strings "True" and "[3726, 3731]" in the retrofitted library, and a
truthiness check that misses that form silently audits zero lines while
reporting a clean run.
"""
import unittest

from experiments.production_trigram_audit import (audit_script, is_spoken,
                                                  load_aliases, span_of)

SOURCE = (
    'The hall was cold.\n\n'
    '"The opposite?" said Subaru, confused.\n\n'
    '"Yes, the opposite," Puck said with a nod.\n'
)
START = SOURCE.index("The opposite?")
ROSTER = ["SUBARU", "PUCK", "SATELLA"]


def entry(text, speaker, start, end, spoken=True):
    return {"text": text, "speaker": speaker, "spoken": spoken,
            "source_span": [start, end]}


class ParsingTest(unittest.TestCase):
    def test_spoken_is_read_from_the_string_form_too(self):
        """The retrofitted library stores it as the string "True"."""
        self.assertTrue(is_spoken({"spoken": True}))
        self.assertTrue(is_spoken({"spoken": "True"}))
        self.assertFalse(is_spoken({"spoken": False}))
        self.assertFalse(is_spoken({"spoken": "False"}))
        self.assertFalse(is_spoken({}))

    def test_the_span_is_read_from_the_string_form_too(self):
        self.assertEqual((3726, 3731), span_of({"source_span": [3726, 3731]}))
        self.assertEqual((3726, 3731), span_of({"source_span": "[3726, 3731]"}))

    def test_a_missing_or_malformed_span_yields_none_not_a_crash(self):
        for bad in (None, "", "not a span", [1], {"a": 1}):
            self.assertIsNone(span_of({"source_span": bad}), bad)
        self.assertIsNone(span_of({}))

    def test_aliases_become_scoring_groups(self):
        groups = load_aliases("/does/not/exist.json")
        self.assertEqual([], groups, "a missing alias file must not raise")


class AuditTest(unittest.TestCase):
    def _audit(self, entries):
        return audit_script(entries, SOURCE, ROSTER, [])

    def test_a_disagreement_is_reported_with_the_text_that_caused_it(self):
        counts, rows = self._audit(
            [entry("The opposite?", "SATELLA", START, START + 13)])
        self.assertEqual(1, counts["fired"])
        self.assertEqual(1, counts["disagree"])
        self.assertEqual("SUBARU", rows[0]["rule"])
        self.assertIn("said Subaru", rows[0]["after"])

    def test_agreement_is_counted_and_not_listed(self):
        counts, rows = self._audit(
            [entry("The opposite?", "SUBARU", START, START + 13)])
        self.assertEqual(1, counts["agree"])
        self.assertEqual(0, counts["disagree"])
        self.assertEqual([], rows)

    def test_an_unattributed_line_the_rule_can_name_is_a_disagreement(self):
        """NARRATOR on a spoken line is a gap, not an agreement."""
        counts, _ = self._audit(
            [entry("The opposite?", "NARRATOR", START, START + 13)])
        self.assertEqual(1, counts["fired_on_unattributed"])
        self.assertEqual(1, counts["disagree"])

    def test_a_line_that_is_not_spoken_is_never_audited(self):
        counts, _ = self._audit(
            [entry("The opposite?", "SATELLA", START, START + 13, spoken=False)])
        self.assertEqual(0, counts["spoken"])
        self.assertEqual(0, counts["fired"])

    def test_a_spoken_line_with_no_span_counts_as_spoken_but_not_audited(self):
        """It must not silently vanish from the denominator."""
        counts, _ = self._audit([{"text": "x", "speaker": "SUBARU",
                                  "spoken": True}])
        self.assertEqual(1, counts["spoken"])
        self.assertEqual(0, counts["with_span"])

    def test_an_alias_of_the_assigned_name_is_agreement_not_a_change(self):
        groups = [{"SUBARU", "SUBARU NATSUKI"}]
        counts, _ = audit_script(
            [entry("The opposite?", "SUBARU NATSUKI", START, START + 13)],
            SOURCE, ROSTER, groups)
        self.assertEqual(1, counts["agree"])
