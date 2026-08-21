"""Carrying a split quotation's narration without disturbing anything measured.

`"Bah!" said Scrooge, "Humbug!"` is ONE quotation with two parts, and
`said Scrooge` sits in the gap between them - inside the envelope the context
window is measured from, so it appeared in neither context, while `line` is the
joined quote text without it. 31.3% of PDNC quotations are multi-part and it
cost 11.0 points on Explicit quotes (#384).

The danger in fixing it is not the fix, it is the REGENERATION. Every number in
the ledger was measured against `line`, `prev_context` and `next_context`. If a
rebuild moves them by one byte, the control arm becomes a different experiment
and every prior comparison is void. So `inner_narration` is additive and the
regeneration is asserted to leave the old fields alone - measured on the real
fixtures at the time: 2,494 entries, 0 changed, 780 gained narration.

Verified by the artifact moving rather than by a green test: Explicit rows with
the annotator's own referring expression visible go from 294 of 543 to 539.
"""
import unittest

from experiments.pdnc_gold_rebuild import envelope, inner_narration
from experiments.two_stage_attribution import PROMPT_VARIANTS, build_prompt

TEXT = 'He said. "Bah!" said Scrooge, "Humbug!" The clerk shivered.'
BAH = (TEXT.index('"Bah!"'), TEXT.index('"Bah!"') + len('"Bah!"'))
HUM = (TEXT.index('"Humbug!"'), TEXT.index('"Humbug!"') + len('"Humbug!"'))


class SpanTest(unittest.TestCase):
    def test_the_envelope_spans_first_start_to_last_end(self):
        self.assertEqual((BAH[0], HUM[1]), envelope([BAH, HUM]))

    def test_a_single_part_quotation_is_its_own_envelope(self):
        self.assertEqual(BAH, envelope([BAH]))


class InnerNarrationTest(unittest.TestCase):
    def test_the_gap_between_parts_is_recovered(self):
        self.assertEqual(" said Scrooge, ", inner_narration(TEXT, [BAH, HUM]))

    def test_a_single_part_quotation_has_none(self):
        self.assertEqual("", inner_narration(TEXT, [BAH]))

    def test_abutting_parts_are_indistinguishable_from_a_whole_quote(self):
        """Empty must mean empty, not 'there was a gap of length zero'."""
        text = '"Bah!""Humbug!"'
        parts = [(0, 6), (6, 15)]
        self.assertEqual("", inner_narration(text, parts))

    def test_three_parts_yield_both_gaps_in_order(self):
        text = '"a" one "b" two "c"'
        parts = [(0, 3), (8, 11), (16, 19)]
        self.assertEqual(" one  two ", inner_narration(text, parts))


class PromptVariantTest(unittest.TestCase):
    ENTRY = {"id": "Q1", "line": "Bah! Humbug!", "prev_context": "before",
             "next_context": "after", "inner_narration": " said Scrooge, "}

    def test_the_variant_is_offered(self):
        self.assertIn("inner_narration", PROMPT_VARIANTS)

    def test_this_arm_is_offered(self):
        """The exact tuple is pinned once, in test_prompt_variant.

        It was pinned HERE too, and both copies had to be edited every time an
        arm was added - two independently-maintained statements of one
        decision, which is the drift Rule 15 is about, occurring inside the
        tests written to prevent it. This file now asserts only what it is
        about: that `inner_narration` exists.
        """
        self.assertIn("inner_narration", PROMPT_VARIANTS)

    def test_control_is_untouched_by_any_of_this(self):
        """The whole ledger depends on this staying byte-identical."""
        without = dict(self.ENTRY)
        without.pop("inner_narration")
        self.assertEqual(build_prompt(without, ["A", "B"]),
                         build_prompt(self.ENTRY, ["A", "B"]))

    def test_the_narration_is_appended_and_labelled(self):
        control = build_prompt(self.ENTRY, ["A", "B"])
        shown = build_prompt(self.ENTRY, ["A", "B"], variant="inner_narration")
        self.assertTrue(shown.startswith(control))
        self.assertIn("said Scrooge", shown[len(control):])
        self.assertIn("interrupting", shown[len(control):])

    def test_a_single_part_quote_gets_no_extra_block(self):
        """Two thirds of rows have nothing to add; they must be unchanged."""
        entry = dict(self.ENTRY, inner_narration="")
        self.assertEqual(build_prompt(entry, ["A", "B"]),
                         build_prompt(entry, ["A", "B"], variant="inner_narration"))

    def test_a_fixture_without_the_field_does_not_crash_the_arm(self):
        entry = {"id": "Q2", "line": "x", "prev_context": "a", "next_context": "b"}
        self.assertEqual(build_prompt(entry, ["A"]),
                         build_prompt(entry, ["A"], variant="inner_narration"))
