"""The model names the listener, and the two-hander that would fake it.

PDNC annotates an addressee for every quotation; this project had never opened
the column. Of the wrong rows carrying one, 78.5% named an addressee - but in a
two-person scene the addressee is the ONLY alternative, so that number alone
proves nothing. The measurement that carries the finding excludes those rows:

    rows where an addressee AND another present person were both available: 798
      chose the addressee              621   77.8%
      chose another present character  110   13.8%
      chose someone absent              67    8.4%
      random among present people             41.2%

The exclusion is the load-bearing part, so it is tested first.
"""
import unittest

from experiments.addressee_confusion import addressees, classify
from experiments.two_stage_attribution import (PROMPT_VARIANTS,
                                               SPEAKER_NOT_ADDRESSEE,
                                               build_prompt)

GROUPS = [{"ELIZABETH", "LIZZY"}]


class AddresseeParsingTest(unittest.TestCase):
    def test_the_annotated_list_is_read(self):
        self.assertEqual(["Mr. Bennet"],
                         addressees({"addressees": "['Mr. Bennet']"}))

    def test_several_addressees_are_kept(self):
        self.assertEqual(["Jane", "Lydia"],
                         addressees({"addressees": "['Jane', 'Lydia']"}))

    def test_an_unusable_column_is_empty_not_a_guess(self):
        for bad in ({}, {"addressees": ""}, {"addressees": "not a list"},
                    {"addressees": "'a string'"}):
            self.assertEqual([], addressees(bad), bad)

    def test_blank_entries_are_dropped(self):
        self.assertEqual(["Jane"], addressees({"addressees": "['Jane', '']"}))


class ClassifyTest(unittest.TestCase):
    PRESENT = ["MR. BENNET", "MRS. BENNET", "JANE", "LYDIA"]

    def test_naming_the_addressee_is_recognised(self):
        self.assertEqual("addressee", classify(
            "MRS. BENNET", "MR. BENNET", ["Mrs. Bennet"], self.PRESENT, GROUPS))

    def test_naming_a_different_present_person_is_separated(self):
        """This is the cell that would be large if it were mere proximity."""
        self.assertEqual("other_present", classify(
            "LYDIA", "MR. BENNET", ["Mrs. Bennet"], self.PRESENT, GROUPS))

    def test_naming_somebody_not_present_is_its_own_cell(self):
        self.assertEqual("absent", classify(
            "MR. DARCY", "MR. BENNET", ["Mrs. Bennet"], self.PRESENT, GROUPS))

    def test_the_gold_speaker_is_never_counted_as_another_present_person(self):
        """A wrong row cannot have named the right person."""
        self.assertEqual("absent", classify(
            "MR. BENNET", "MR. BENNET", ["Mrs. Bennet"], self.PRESENT, GROUPS))

    def test_an_alias_of_the_addressee_still_counts_as_the_addressee(self):
        self.assertEqual("addressee", classify(
            "LIZZY", "JANE", ["Elizabeth"], ["ELIZABETH", "JANE"], GROUPS))


class ArmTest(unittest.TestCase):
    ENTRY = {"id": "Q1", "line": "x", "prev_context": "a", "next_context": "b"}

    def test_the_arm_is_offered(self):
        """The exact tuple is pinned once, in test_prompt_variant.

        Writing this file I re-created the duplicate I had just removed from
        test_split_quote_repair - two copies of one decision, again. Each arm
        test asserts only that its own arm exists.
        """
        self.assertIn("speaker_not_addressee", PROMPT_VARIANTS)

    def test_it_is_a_pure_suffix_so_control_is_untouched(self):
        control = build_prompt(self.ENTRY, ["A", "B"])
        armed = build_prompt(self.ENTRY, ["A", "B"],
                             variant="speaker_not_addressee")
        self.assertTrue(armed.startswith(control))
        self.assertEqual(SPEAKER_NOT_ADDRESSEE, armed[len(control):])

    def test_it_names_the_error_it_is_correcting(self):
        self.assertIn("spoken to", SPEAKER_NOT_ADDRESSEE)
        self.assertIn("LISTENER", SPEAKER_NOT_ADDRESSEE)

    def test_an_unknown_variant_is_still_refused(self):
        with self.assertRaises(ValueError):
            build_prompt(self.ENTRY, ["A"], variant="speaker_not_adressee")
