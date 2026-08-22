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

from experiments.addressee_confusion import (addressees, classify, corpus_key,
                                             entry_addressees, quote_order,
                                             row_id, separate_addressee_from_persistence,
                                             speaker_index, turn_neighbours)
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


class TurnNeighbourTests(unittest.TestCase):
    """The ordering helpers, on the cases that decide the verdict."""

    GROUPS = {}

    def test_ends_of_a_scene_have_one_neighbour(self):
        # `speakers` is now a FLAT {quote id: speaker} map, not the raw row
        # shape, so the same function serves PDNC (built from its corpus) and
        # RiQuA (built from fixture entries).
        order = ["Q0", "Q1", "Q2"]
        speakers = {"Q0": "Ann", "Q1": "Bob", "Q2": "Ann"}
        self.assertEqual(turn_neighbours(order, 0, speakers), (None, "Bob"))
        self.assertEqual(turn_neighbours(order, 2, speakers), ("Bob", None))
        self.assertEqual(turn_neighbours(order, 1, speakers), ("Ann", "Ann"))

    def test_two_party_exchange_cannot_separate_the_hypotheses(self):
        # Ann and Bob alternate, so the addressee IS the previous speaker.
        # This is the majority case and it must count as uninformative, not
        # as support for either explanation.
        self.assertIsNone(separate_addressee_from_persistence(
            "Ann", ["Ann"], "Ann", self.GROUPS))

    def test_addressee_who_did_not_speak_last_supports_addressee(self):
        # Three in the room: Cid spoke last, but the line is aimed at Ann,
        # and the model said Ann.
        self.assertEqual(
            separate_addressee_from_persistence("Ann", ["Ann"], "Cid",
                                                   self.GROUPS),
            "addressee_not_previous_speaker")

    def test_last_speaker_who_is_not_addressed_supports_persistence(self):
        self.assertEqual(
            separate_addressee_from_persistence("Cid", ["Ann"], "Cid",
                                                   self.GROUPS),
            "previous_speaker_not_addressee")

    def test_naming_a_third_party_supports_neither(self):
        self.assertIsNone(separate_addressee_from_persistence(
            "Dot", ["Ann"], "Cid", self.GROUPS))

    def test_missing_previous_speaker_is_not_a_match(self):
        # First quote of a book has no predecessor; it must not be scored as
        # persistence-agreeing just because prev is falsy.
        self.assertEqual(
            separate_addressee_from_persistence("Ann", ["Ann"], None,
                                                   self.GROUPS),
            "addressee_not_previous_speaker")

    def test_aliases_resolve_through_the_group_map(self):
        groups = {"subaru": "Subaru", "subaru natsuki": "Subaru"}
        self.assertIsNone(separate_addressee_from_persistence(
            "Subaru Natsuki", ["Subaru"], "Subaru", groups))


class CorpusAgnosticSourceTests(unittest.TestCase):
    """Addressees come from whichever corpus carries them, via ONE dispatch.

    The analysis was written against PDNC, whose fixtures do NOT carry
    addressees - the names live in the raw corpus, keyed by pdnc_quote_id.
    RiQuA marks the relation directly and its reader keeps it inline. Rather
    than a second copy of the analysis per corpus, both go through
    `entry_addressees`, so the 205-v-4 separation is computed identically on
    either and a third corpus needs no new code.
    """

    def test_an_inline_field_is_used(self):
        entry = {"id": "x-1", "addressees": ["Mr. Knightley"]}
        self.assertEqual(entry_addressees(entry, None), ["Mr. Knightley"])

    def test_inline_wins_over_the_raw_corpus(self):
        # A fixture stating its own addressees is the more specific answer.
        entry = {"id": "x-1", "addressees": ["EMMA"], "pdnc_quote_id": "Q1"}
        raw = {"Q1": {"addressees": "['HARRIET']"}}
        self.assertEqual(entry_addressees(entry, raw), ["EMMA"])

    def test_pdnc_rows_fall_back_to_the_raw_corpus(self):
        # PDNC's column is a STRING holding a python literal, straight out of
        # the corpus CSV - not a list of dicts. A first version of this test
        # invented the wrong shape and failed against working code.
        entry = {"id": "x-1", "pdnc_quote_id": "Q1"}
        raw = {"Q1": {"addressees": "['HARRIET']"}}
        self.assertEqual(entry_addressees(entry, raw), ["HARRIET"])

    def test_a_row_with_neither_yields_nothing(self):
        # Must be empty, not a crash and not a fabricated name: the caller
        # skips such rows rather than counting them.
        self.assertEqual(entry_addressees({"id": "x-1"}, None), [])
        self.assertEqual(entry_addressees({"id": "x-1"}, {}), [])

    def test_an_empty_inline_list_falls_through(self):
        entry = {"id": "x-1", "addressees": [], "pdnc_quote_id": "Q1"}
        raw = {"Q1": {"addressees": "['HARRIET']"}}
        self.assertEqual(entry_addressees(entry, raw), ["HARRIET"])

    def test_blank_names_are_dropped_from_an_inline_list(self):
        entry = {"id": "x-1", "addressees": ["EMMA", "", None]}
        self.assertEqual(entry_addressees(entry, None), ["EMMA"])

    def test_pdnc_order_comes_from_the_raw_corpus(self):
        raw = {"Q10": {}, "Q2": {}, "Q1": {}}
        self.assertEqual(quote_order("bk", [], raw), ["Q1", "Q2", "Q10"])

    def test_a_fixture_without_raw_data_orders_by_its_own_entries(self):
        # RiQuA readers emit entries in document order, so the sequence IS
        # the order and the entry ids identify rows.
        entries = [{"id": "a-1"}, {"id": "a-2"}, {"id": "a-3"}]
        self.assertEqual(quote_order("bk", entries, None), ["a-1", "a-2", "a-3"])

    def test_entries_without_ids_are_skipped_not_padded(self):
        entries = [{"id": "a-1"}, {}, {"id": "a-3"}]
        self.assertEqual(quote_order("bk", entries, None), ["a-1", "a-3"])


class BookIdentityTests(unittest.TestCase):
    """Fixtures from a corpus with no raw data must stay separate books.

    `match_book` resolves against PDNC's corpus, so it returned the same value
    for all fifteen RiQuA fixtures. They collapsed into one book, only the
    first text ever had a document order, and the separation ran on 5 rows of
    1,537 - printing `2 / 1 / 2`, which reads like a weak real result and was
    actually 79 / 10 with 99.7% of the data invisible.
    """

    def test_corpus_key_strips_the_corpus_name(self):
        self.assertEqual(corpus_key("attribution_gold_pdnc_prideandprejudice_w3200"),
                         "prideandprejudice_w3200")
        self.assertEqual(corpus_key("attribution_gold_riqua_austen_emma_1"),
                         "austen_emma_1")

    def test_an_unknown_corpus_keeps_its_stem(self):
        self.assertEqual(corpus_key("attribution_gold_newcorpus_book"),
                         "newcorpus_book")

    def test_row_id_prefers_the_pdnc_field_then_falls_back(self):
        self.assertEqual(row_id({"pdnc_quote_id": "Q7", "id": "x-1"}), "Q7")
        self.assertEqual(row_id({"id": "x-1"}), "x-1")
        self.assertIsNone(row_id({}))

    def test_speaker_index_reads_raw_when_present(self):
        raw = {"Q0": {"speaker": "ANN"}, "Q1": {"speaker": "BOB"}}
        self.assertEqual(speaker_index([], raw), {"Q0": "ANN", "Q1": "BOB"})

    def test_speaker_index_falls_back_to_fixture_entries(self):
        entries = [{"id": "a-1", "expected_speaker": "EMMA"},
                   {"id": "a-2", "expected_speaker": "HARRIET"}]
        self.assertEqual(speaker_index(entries, None),
                         {"a-1": "EMMA", "a-2": "HARRIET"})
