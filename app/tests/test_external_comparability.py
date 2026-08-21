"""Every external number this project cites must carry its protocol.

Reading the papers on 2026-08-21 showed the figures quoted here as targets were
produced under conditions we do not share. The case that settles it is one
paper reporting zero-shot GPT-3.5 at 10.9% on RiQua and 70.07% on JY-QuotePlus
- sixty points, same model, same task, different corpus and scoring.

So a number without its protocol is not a number, and this file refuses to let
one into the record. `comparable_to_ours: False` with a reason is the useful
state; the failure mode is an entry that looks authoritative and cannot be
placed beside anything.
"""
import unittest

from experiments.external_comparability import (RECORD, REQUIRED, incomparable,
                                                missing_fields)


class RecordShapeTest(unittest.TestCase):
    def test_every_entry_carries_every_required_field(self):
        bad = {e.get("claim", "?"): missing_fields(e) for e in RECORD
               if missing_fields(e)}
        self.assertEqual({}, bad)

    def test_a_number_without_a_protocol_is_rejected(self):
        self.assertIn("protocol", missing_fields(
            {"claim": "x", "value": 0.9, "source": "s", "dataset": "d",
             "comparable_to_ours": True, "why": "w"}))

    def test_a_complete_entry_reports_nothing_missing(self):
        self.assertEqual([], missing_fields({k: "x" for k in REQUIRED}))


class ContentTest(unittest.TestCase):
    def _by(self, needle):
        return [e for e in RECORD if needle in e["source"]]

    def test_the_gold_mention_results_are_marked_incomparable(self):
        """They resolve for free what our arm has to infer."""
        for entry in self._by("2608.02359"):
            self.assertFalse(entry["comparable_to_ours"], entry["claim"])
            # Case-insensitive: the substance is that the protocol SAYS gold
            # mentions, not how it is capitalised. The first version demanded
            # upper case and failed on the entry that says "as above, gold
            # mentions" - a test failing on its own formatting, not the record.
            self.assertIn("gold", entry["protocol"].lower())

    def test_elsons_number_is_the_one_that_transfers(self):
        """Because we reproduced it ourselves rather than quoting it."""
        elson = self._by("Elson")
        self.assertEqual(1, len(elson))
        self.assertTrue(elson[0]["comparable_to_ours"])
        self.assertIn("9899", elson[0]["why"])

    def test_the_sixty_point_spread_is_recorded_as_one_paper(self):
        """Two corpora, one model, one protocol - the whole argument."""
        pair = self._by("2408.09452")
        self.assertEqual(2, len(pair))
        values = sorted(e["value"] for e in pair)
        self.assertAlmostEqual(0.109, values[0], 3)
        self.assertGreater(values[1] - values[0], 0.5)
        for entry in pair:
            self.assertFalse(entry["comparable_to_ours"])

    def test_our_own_number_is_marked_comparable_to_itself(self):
        ours = [e for e in RECORD if e["dataset"].startswith("PDNC, 2494")]
        self.assertEqual(1, len(ours))
        self.assertTrue(ours[0]["comparable_to_ours"])
        self.assertAlmostEqual(0.656, ours[0]["value"], 3)

    def test_most_external_numbers_do_not_transfer(self):
        """If this ever flips, the claim in the docstring needs rewriting."""
        self.assertGreater(len(incomparable(RECORD)), len(RECORD) / 2)
