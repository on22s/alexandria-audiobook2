"""Money with cents, rising ranges and slash dates are spoken as a reader would (#1546).

Every converted case below is a line the narrator voice misread in a blind
listening round; every left-alone case is a shape the rules must not touch,
most of them real text from saved books (ISBNs, product codes).
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import speech_numbers
import speech_text
from speech_numbers import (get_book_date_order_status, get_date_order_evidence,
                            verbalize_numbers)


def spoken(text, order=None):
    return verbalize_numbers(text, order)[0]


class ConvertedTests(unittest.TestCase):
    def test_every_listened_failure_is_reshaped(self):
        cases = {
            "She paid $4.50 for the ribbon.": "She paid 4 dollars and 50 cents for the ribbon.",
            "The fare was only $0.75.": "The fare was only 75 cents.",
            "She found $2.05 in her pocket.": "She found 2 dollars and 5 cents in her pocket.",
            "A loaf cost $1.10.": "A loaf cost 1 dollar and 10 cents.",
            "It cost $1,250.75.": "It cost 1,250 dollars and 75 cents.",
            "It was $5.00 even.": "It was 5 dollars even.",
            "Read chapters 3-5 tonight.": "Read chapters 3 to 5 tonight.",
            "See pages 45-48 for the map.": "See pages 45 to 48 for the map.",
            "Questions 1-10 are easy.": "Questions 1 to 10 are easy.",
            "The meeting runs 9–11 tomorrow.": "The meeting runs 9 to 11 tomorrow.",
            "Read pages 10-12 before Monday.": "Read pages 10 to 12 before Monday.",
            "He lived there from 1890-1902.": "He lived there from 1890 to 1902.",
            "On 3/14/1879 he was born.": "On the 14th of March, 1879 he was born.",
            "The ship sailed on 6/30/1912.": "The ship sailed on the 30th of June, 1912.",
            "By 11/11/1918 it was over.": "By the 11th of November, 1918 it was over.",
            "Dated 25/12/1890.": "Dated the 25th of December, 1890.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, spoken(source))

    def test_an_ambiguous_date_follows_the_book_order(self):
        self.assertEqual("dated the 4th of July, 1776.", spoken("dated 7/4/1776.", "mdy"))
        self.assertEqual("dated the 7th of April, 1776.", spoken("dated 7/4/1776.", "dmy"))

    def test_every_change_is_recorded(self):
        _text, changes = verbalize_numbers("Pay $3.25 for pages 3-5.")
        self.assertEqual([{"type": "verbalized_numbers", "changes": [
            {"kind": "money", "source": "$3.25", "spoken": "3 dollars and 25 cents"},
            {"kind": "range", "source": "3-5", "spoken": "3 to 5"}]}], changes)


class LeftAloneTests(unittest.TestCase):
    def test_codes_scores_fractions_and_plain_amounts_are_untouched(self):
        for text in ("ISBNs 978-0-316-31530-2 (paperback)", "Build E3-20180216-JV-PC.",
                     "Call 555-1234 tonight.", "They won 5-3.", "Pages 07-09.",
                     "The 1914-18 war.", "pages 1.2-3.4", "about 70/30 odds",
                     "Only 1/3 of the flour.", "On 2/3/90 it rained.", "On 13/13/1900.",
                     "On 2/30/1900.", "The ticket was £3.50.", "It cost $5.", "It cost $1,000,000.",
                     "version 2.5-3", "a 3-5-7 pattern", "ratio 1/2/3",
                     # Rising, but nothing beside them says "range".
                     "The 737-800 entered service.", "He fell behind on a 1-2 pitch.",
                     "Subtract 3-5."):
            with self.subTest(text=text):
                self.assertEqual((text, []), verbalize_numbers(text))

    def test_an_ambiguous_date_with_no_order_is_left_and_recorded(self):
        text, changes = verbalize_numbers("She wrote on 1/2/1900.")
        self.assertEqual("She wrote on 1/2/1900.", text)
        self.assertEqual([{"type": "ambiguous_date", "count": 1, "date_order": None}], changes)


class EvidenceTests(unittest.TestCase):
    def test_the_books_own_dates_decide_before_spelling(self):
        british = " ".join(["colour honour centre"] * 5)
        self.assertEqual(("mdy", "book_dates"),
                         tuple(get_date_order_evidence(f"On 12/25/1890. {british}")[k] for k in ("order", "source")))
        self.assertEqual(("dmy", "book_dates"),
                         tuple(get_date_order_evidence("On 25/12/1890 and 31/1/1900.")[k] for k in ("order", "source")))

    def test_spelling_decides_only_with_enough_hits_and_a_clear_margin(self):
        self.assertEqual("dmy", get_date_order_evidence("colour honour grey " * 3)["order"])
        self.assertEqual("mdy", get_date_order_evidence("color honor gray " * 3)["order"])
        self.assertIsNone(get_date_order_evidence("colour color honour honor " * 3)["order"])
        self.assertIsNone(get_date_order_evidence("colour honour grey")["order"])

    def test_conflicting_dates_are_undetermined_even_with_spelling(self):
        result = get_date_order_evidence("12/25/1890 and 25/12/1890. " + "colour " * 20)
        self.assertEqual((None, "undetermined"), (result["order"], result["source"]))
        self.assertEqual({"mdy": 1, "dmy": 1}, result["detail"]["date_votes"])


class BookOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, state=None, script=None):
        if state is not None:
            (self.root / "state.json").write_text(state if isinstance(state, str) else json.dumps(state))
        if script is not None:
            (self.root / "annotated_script.json").write_text(json.dumps([{"text": t} for t in script]))
        time.sleep(0.01)    # a new mtime, as a real edit would have

    def test_choice_overrides_auto_and_auto_uses_the_script(self):
        self.write({"active_book_id": "b"}, ["On 12/25/1890 it snowed.", "1/2/1900"])
        status = get_book_date_order_status(str(self.root))
        self.assertEqual(("auto", "mdy"), (status["choice"], status["order"]))
        self.assertEqual(1, status["evidence"]["detail"]["ambiguous_dates"])
        self.write({"active_book_id": "b", "speech_date_order": "dmy"})
        self.assertEqual("dmy", speech_numbers.get_book_date_order(str(self.root)))

    def test_missing_or_malformed_state_falls_back_to_auto_never_a_guess(self):
        self.assertIsNone(speech_numbers.get_book_date_order(str(self.root)))
        self.write("{broken", ["1/2/1900"])
        self.assertEqual(("auto", None), tuple(get_book_date_order_status(str(self.root))[k]
                                               for k in ("choice", "order")))
        self.write({"speech_date_order": "ymd"})
        self.assertEqual("auto", get_book_date_order_status(str(self.root))["choice"])

    def test_speech_preparation_reads_the_book_order_and_lexicon_still_runs_last(self):
        with patch.object(speech_text, "get_book_date_order", return_value="dmy"):
            with patch("pronunciation.apply_pronunciation", side_effect=lambda text: (text.replace("April", "Aprill"), [{"name": "April", "spoken": "Aprill"}])):
                result = speech_text.get_speech_normalization("Dated 7/4/1776 at 3-5 pm, $2.05 paid.")
        self.assertEqual("Dated the 7th of Aprill, 1776 at 3 to 5 pm, 2 dollars and 5 cents paid.", result["text"])
        self.assertIn("verbalized_numbers", [change["type"] for change in result["transformations"]])


if __name__ == "__main__":
    unittest.main()
