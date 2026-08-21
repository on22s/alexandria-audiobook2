"""A quotation split by narration loses its attribution before the model sees it.

`"Bah!" said Scrooge, "Humbug!"` is ONE quotation in PDNC with two parts.
`pdnc_gold_rebuild.spans` collapses it to min(start), max(end), so
`prev_context` stops before "Bah! and `next_context` starts after Humbug!" -
and `said Scrooge`, which sits between the parts, is in neither. `line` is the
joined quote text without it.

Measured on 2,494 rows: 31.3% of quotations are multi-part, and among EXPLICIT
ones the annotator's referring expression is absent from everything the model
sees 69.1% of the time (246 of 356) against 1.6% for single-part quotes. The
cost is 11.0 points of accuracy. Anaphoric loses 0.6 - the control that says
this is about losing a NAME, since an anaphoric referring expression is a
pronoun and worth nothing anyway.
"""
import unittest

from experiments.split_quote_evidence import evidence_location, part_count


def entry(line="", prev="", nxt=""):
    return {"line": line, "prev_context": prev, "next_context": nxt}


class PartCountTest(unittest.TestCase):
    def test_a_split_quotation_reports_its_parts(self):
        self.assertEqual(2, part_count(
            {"subQuotationList": "['Bah!', 'Humbug!']"}))

    def test_a_whole_quotation_is_one_part(self):
        self.assertEqual(1, part_count({"subQuotationList": "['Bah!']"}))

    def test_a_missing_or_unparsable_list_is_one_part(self):
        """Assuming 'split' on bad data would invent the defect being measured."""
        self.assertEqual(1, part_count({}))
        self.assertEqual(1, part_count({"subQuotationList": ""}))
        self.assertEqual(1, part_count({"subQuotationList": "not a list"}))
        self.assertEqual(1, part_count({"subQuotationList": "[]"}))


class EvidenceLocationTest(unittest.TestCase):
    ROW = {"referringExpression": "said Scrooge"}

    def test_evidence_in_the_surrounding_context_is_found(self):
        self.assertEqual("context", evidence_location(
            self.ROW, entry(line="Humbug!", nxt=' said Scrooge, warming himself.')))

    def test_evidence_inside_the_line_is_reported_separately(self):
        """It would mean the joined line kept the narration - it does not."""
        self.assertEqual("line", evidence_location(
            self.ROW, entry(line='Bah! said Scrooge, Humbug!')))

    def test_the_split_quote_shape_reports_absent(self):
        """The defect: narration between the parts is in neither field."""
        self.assertEqual("absent", evidence_location(
            self.ROW, entry(line="Bah! Humbug!",
                            prev="Scrooge sat by the fire. ",
                            nxt=" The clerk said nothing.")))

    def test_a_quotation_with_no_referring_expression_is_none(self):
        """None means unmeasurable here, not 'evidence absent'."""
        self.assertIsNone(evidence_location({"referringExpression": ""}, entry()))
        self.assertIsNone(evidence_location({}, entry()))

    def test_punctuation_and_case_do_not_defeat_the_match(self):
        self.assertEqual("context", evidence_location(
            {"referringExpression": "cried Elizabeth"},
            entry(nxt='"  --  CRIED ELIZABETH!')))
