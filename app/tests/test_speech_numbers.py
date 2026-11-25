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
        # A chunk holding many ranges reads each one exactly as it reads alone.
        unit = "See pages 3-5.\tThe 737-800 flew 9–11 pm; subtract 1-2. "
        self.assertEqual(spoken(unit) * 800, spoken(unit * 800))
        # Range context is read from each range's own neighbours, never by copying
        # or searching the whole text around it: reads stay proportional to length.
        class CountedText(str):
            read = 0
            def __getitem__(self, key):
                part = str.__getitem__(self, key)
                CountedText.read += len(part)
                return part
        for repeats in (100, 800):
            text = CountedText("pages 3-5. " * repeats)
            CountedText.read = 0
            for match in speech_numbers._RANGE_RE.finditer(text):
                self.assertTrue(speech_numbers._has_range_context(match))
            self.assertLessEqual(CountedText.read, 2 * len(text), repeats)

    def test_every_currency_prefix_is_verbalized_with_its_spoken_name(self):
        # Every code in the table, with the word a listener should hear. The
        # issue's own example (US$) keeps the literal "US" wording.
        cases = {
            "US": "US", "CAD": "Canadian", "CA": "Canadian", "C": "Canadian",
            "A": "Australian", "AU": "Australian", "HK": "Hong Kong",
            "NZ": "New Zealand", "S": "Singapore", "SG": "Singapore",
            "NT": "New Taiwan",
        }
        self.assertEqual(cases, speech_numbers._SPOKEN_CURRENCIES)
        for code, currency in cases.items():
            with self.subTest(code=code):
                self.assertEqual(f"It cost 12 {currency} dollars and 50 cents.",
                                 spoken(f"It cost {code}$12.50."))
        self.assertEqual("It cost 1,234 US dollars and 56 cents today.",
                         spoken("It cost US$1,234.56 today."))
        self.assertEqual("It cost 1 US dollar.", spoken("It cost US$1.00."))
        self.assertEqual("It cost 1 US dollar and 1 cent.", spoken("It cost US$1.01."))
        # Whole dollars at zero keep the existing "$" rule, which names the cents only.
        self.assertEqual("It cost 75 cents.", spoken("It cost US$0.75."))

    def test_prefixed_amount_followed_by_a_magnitude_word_is_left_as_written(self):
        # "US$5.50 million" is five and a half million, not fifty cents; main
        # leaves the whole prefixed form raw, so this rule must not turn it into
        # "...and 50 cents million". With or without a space.
        for text in ("It cost US$5.50 million.", "It cost US$5.50million.",
                     "It cost US$5.50 millions.", "It cost US$5.50 billion.",
                     "It cost US$5.50 trillion.", "It cost US$5.50 thousand.",
                     "It cost US$5.50 hundred.", "It cost US$5.50 bn.",
                     "It cost US$5.50 m.", "It cost US$5.50 k.",
                     "It cost CAD$12.50 million."):
            with self.subTest(text=text):
                self.assertEqual((text, []), verbalize_numbers(text))
        # A word that is not a magnitude still converts.
        self.assertEqual("It cost 5 US dollars and 50 cents in cash.",
                         spoken("It cost US$5.50 in cash."))
        # The plain "$" rule uses the same guard, so it stays as written too.
        self.assertEqual("It cost $5.50 million.",
                         spoken("It cost $5.50 million."))

    def test_a_prefixed_amount_too_long_to_read_is_left_as_written(self):
        # Digits on the prefixed rule are bounded so an absurd amount is left
        # alone instead of raising inside int(); main left every prefixed form
        # untouched too. The plain "$" rule keeps its existing unbounded form.
        for text in ("It cost US$" + "1" * 5000 + ".00.",
                     "It cost US$" + ",".join(["123"] * 10) + ".00."):
            with self.subTest(length=len(text)):
                self.assertEqual((text, []), verbalize_numbers(text))
        self.assertEqual("It cost 123,123,123,123,123,123 US dollars.",
                         spoken("It cost US$123,123,123,123,123,123.00."))

    def test_prefixed_money_change_is_recorded(self):
        _text, changes = verbalize_numbers("It cost US$1,234.56 today.")
        self.assertEqual([{"type": "verbalized_numbers", "changes": [
            {"kind": "money", "source": "US$1,234.56",
             "spoken": "1,234 US dollars and 56 cents"}]}], changes)

    def test_season_year_range_is_verbalized(self):
        cases = {
            "The 2000-2001 season was long.": "The 2000 to 2001 season was long.",
            "The 2000-2001 seasons were long.": "The 2000 to 2001 seasons were long.",
            "The 1999-2000 season was long.": "The 1999 to 2000 season was long.",
            "The 2000-2001 season, then.": "The 2000 to 2001 season, then.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, spoken(source))

    def test_every_converted_line_is_stable_when_read_again(self):
        for source in ("It cost US$1,234.56 today.", "It cost CAD$12.50.",
                       "The 2000-2001 season was long.", "It cost $4.50 today.",
                       "Read pages 3-5 tonight.", "On 3/14/1879 he was born."):
            with self.subTest(source=source):
                once = spoken(source)
                self.assertEqual(once, spoken(once))

    def test_an_ambiguous_date_follows_the_book_order(self):
        self.assertEqual("dated the 4th of July, 1776.", spoken("dated 7/4/1776.", "mdy"))
        self.assertEqual("dated the 7th of April, 1776.", spoken("dated 7/4/1776.", "dmy"))

    def test_every_change_is_recorded(self):
        _text, changes = verbalize_numbers("Pay $3.25 for pages 3-5.")
        self.assertEqual([{"type": "verbalized_numbers", "changes": [
            {"kind": "money", "source": "$3.25", "spoken": "3 dollars and 25 cents"},
            {"kind": "range", "source": "3-5", "spoken": "3 to 5"}]}], changes)


def normalized(text):
    """The real user path: speech_text -> verbalize_numbers -> symbols."""
    with patch.object(speech_text, "get_book_date_order", return_value=None):
        return speech_text.normalize_for_speech(text)


class PlainMoneyMagnitudeTests(unittest.TestCase):
    """#2315: the guard #2289 added for prefixed amounts must be the same one."""

    def test_plain_dollar_followed_by_a_magnitude_word_is_left_as_written(self):
        for text in (
            "It cost $1.25 million.",
            "It cost $1.25 million",
            "A $2.50 billion deal.",
            "It cost $3.75 trillion.",
            "It cost $1.50 thousand.",
            "It cost $1.50 hundred.",
            "It cost $1.25M.",
            "It cost $1.25k.",
            "It cost $2.50B.",
            "It cost $1.25 M.",
            "It cost $1.50 bn.",
            "It cost $1.50 millions.",
            "It cost $1.50\xa0million.",       # no-break space
            "It cost $1.50\u202fmillion.",      # narrow no-break space
            "It cost $1.50\nmillion.",
            "It cost $1.25-million deal.",      # hyphen already bounded this one
        ):
            with self.subTest(text=text):
                expected = text if text.endswith(".") else text + "."
                self.assertEqual(expected, normalized(text))

    def test_a_magnitude_starting_another_word_does_not_guard_the_amount(self):
        cases = {
            "It cost $1.50 millionaire.": "It cost 1 dollar and 50 cents millionaire.",
            "It cost $1.50 thousandth.": "It cost 1 dollar and 50 cents thousandth.",
            "It cost $1.50 billionth.": "It cost 1 dollar and 50 cents billionth.",
            # Punctuation ends the amount: a comma is not a magnitude word.
            "It cost $1.50, million-dollar smile.":
                "It cost 1 dollar and 50 cents, million-dollar smile.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, normalized(source))

    def test_whole_dollar_amounts_are_unchanged(self):
        for text in ("It cost $5 million.", "It cost $5.", "It cost $1,000,000."):
            with self.subTest(text=text):
                self.assertEqual(text, normalized(text))


class MoneyRangeTests(unittest.TestCase):
    """#2315: only a tight cents range is spoken, and both ends are spoken."""

    def test_a_tight_cents_range_speaks_both_ends(self):
        cases = {
            "It cost $1.50-$2.00.": "It cost 1 dollar and 50 cents to 2 dollars.",
            "It cost $1.50\u2013$2.00.": "It cost 1 dollar and 50 cents to 2 dollars.",
            "It cost $1.50-2.00.": "It cost 1 dollar and 50 cents to 2 dollars.",
            "It cost $1.50-$2.": "It cost 1 dollar and 50 cents to 2 dollars.",
            "It cost $1-$2.50.": "It cost 1 dollar to 2 dollars and 50 cents.",
            "It cost $0.50-$0.75.": "It cost 50 cents to 75 cents.",
            "It cost $1,200.50-$1,500.00.":
                "It cost 1,200 dollars and 50 cents to 1,500 dollars.",
            "It cost $1.50-$2.00-$3.00.":
                "It cost 1 dollar and 50 cents to 2 dollars to 3 dollars.",
            # A prefixed first amount gets the same rule as a plain one.
            "It cost US$1.50-$2.00.": "It cost 1 US dollar and 50 cents to 2 dollars.",
            "It cost CA$1.50-$2.00.": "It cost 1 Canadian dollar and 50 cents to 2 dollars.",
            "It cost US$1.50-2.00.": "It cost 1 US dollar and 50 cents to 2 dollars.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, normalized(source))

    def test_a_money_range_change_is_recorded(self):
        _text, changes = verbalize_numbers("It cost $1.50-$2.00.")
        self.assertEqual([{"type": "verbalized_numbers", "changes": [
            {"kind": "money", "source": "$1.50-$2.00",
             "spoken": "1 dollar and 50 cents to 2 dollars"}]}], changes)

    def test_other_dashes_keep_mains_reading(self):
        # Em dash is not a range: both cents amounts convert on their own, as
        # main did. A spaced dash is not a range either; so are the numbers in
        # prose after an amount.
        cases = {
            "It cost $1.50\u2014$2.00 if you count tax.":
                "It cost 1 dollar and 50 cents\u20142 dollars if you count tax.",
            "It cost $1.50 - $2.00.": "It cost 1 dollar and 50 cents - 2 dollars.",
            "It cost $1.50 \u2013 $2.00.": "It cost 1 dollar and 50 cents \u2013 2 dollars.",
            "He owed $5.00\u201410 days late.": "He owed 5 dollars\u201410 days late.",
            "It cost $4.99 \u2014 12 of us paid.":
                "It cost 4 dollars and 99 cents \u2014 12 of us paid.",
            "He owed $5.00 - 3 of them.": "He owed 5 dollars - 3 of them.",
            "It cost $4.99\u20144 times what I paid.":
                "It cost 4 dollars and 99 cents\u20144 times what I paid.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, normalized(source))
        # A dash that is not a range still converts the amount before it.
        self.assertEqual("It cost 1 dollar and 50 cents - that was all.",
                         normalized("It cost $1.50 - that was all."))
        self.assertEqual("It cost -1 dollar and 50 cents.",
                         normalized("It cost -$1.50."))

    def test_a_tight_bare_or_whole_dollar_range_stays_put(self):
        for text in ("It cost $1.50-2.",
                     "It cost $1.50-10 people came.",
                     "It cost $3.50-5 a pound.",
                     "It cost $19.99-2024 edition.",
                     "Score $1.00\u20130.",
                     "It cost $20.00-1.",
                     "It cost $1.50-2.000.",
                     "It cost $1.50-$2.00s.",
                     # No cents anywhere, so this is not a money range.
                     "It cost $5-$10.",
                     "It cost $1-$2.",
                     "It cost $5.00-$10.00.",
                     "It cost $1.00-$2.00.",
                     "It cost $1.00-$1.00.",
                     "It cost $5-10 million.",
                     "It cost $5-$10 million.",
                     # A slash or hyphen rate is a single amount, not a range.
                     "It cost $3.50/hour.",
                     "It cost $3.50-a-night."):
            with self.subTest(text=text):
                self.assertEqual(text, normalized(text))

    def test_ranges_a_reader_cannot_say_stay_whole(self):
        # Magnitude ranges, slashed rates and hyphenated rates are left whole at
        # both ends, so no half-converted range survives.
        for text in ("It cost $1.50-$2.00 million.",
                     "It cost $1.50 - $2.00 million.",
                     "It cost $1.50 \u2013 $2.00 million.",
                     "It cost $1.50 \u2014 $2.00 million.",
                     "from $1.50-$2.00/hour.",
                     "It cost $1.50-$2.00-a-night.",
                     "It cost $-1.50.",
                     "It cost US$1.50-US$2.00.",
                     "It cost US$5.00-10.00.",
                     "It cost US$1.50 - $2.00 million.",
                     "It cost US$1.50-$2.00 million."):
            with self.subTest(text=text):
                self.assertEqual(text, normalized(text))
        # Spaced prefixed-prefixed ranges keep main's behaviour: both ends spoken.
        self.assertEqual("It cost 1 US dollar and 50 cents - 2 US dollars.",
                         normalized("It cost US$1.50 - US$2.00."))
        # A prefixed head with a bare tail keeps main's conversion of the head.
        self.assertEqual("It cost 5 US dollars - 10.00.",
                         normalized("It cost US$5.00 - 10.00."))


class MoneyPairMagnitudeTests(unittest.TestCase):
    """#2315: a partner left because of a magnitude must not leave half a pair."""

    def test_the_first_amount_stays_written_when_its_partner_has_a_magnitude(self):
        for text in ("from $1.50 to $2.00 million.",
                     "between $1.25 and $2.50 billion.",
                     "It cost $1.50 or $2.00 million.",
                     "from US$1.50 to US$2.00 million.",
                     "between US$1.25 and $2.50 billion.",
                     "It cost US$1.50 or $2.00 million."):
            with self.subTest(text=text):
                self.assertEqual(text, normalized(text))

    def test_and_without_between_still_converts_the_first_amount(self):
        self.assertEqual(
            "I gave 1 dollar and 50 cents and $2.00 million came later.",
            normalized("I gave $1.50 and $2.00 million came later."))
        self.assertEqual(
            "I gave 1 US dollar and 50 cents and US$2.00 million came later.",
            normalized("I gave US$1.50 and US$2.00 million came later."))

    def test_to_without_a_magnitude_still_speaks_both_ends(self):
        self.assertEqual("It cost 1 dollar and 50 cents to 2 dollars.",
                         normalized("It cost $1.50 to $2.00."))
        # A partner with no cents is not left because of a magnitude, so the
        # first amount is spoken, exactly as main did.
        self.assertEqual("It cost 1 dollar and 50 cents to $2 million.",
                         normalized("It cost $1.50 to $2 million."))


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

    def test_prefixed_currency_neighbours_are_not_currency_codes(self):
        for text in ("It cost BUS$5.00.", "It cost SAUS$5.00.", "It cost R$5.00.",
                     "It cost us$5.00.", "It cost US$-5.00.", "It cost US$5-10.",
                     "It cost US$5.00-10.00.",
                     "It cost US$1.234.", "It cost US$1.5.",
                     # Same token boundary as the range/date rules, plus "$":
                     # a preceding letter, digit, period, slash, dash or en dash
                     # keeps the form as written, so dotted initials and paths
                     # never become "US dollars". "-US$5.00" stays raw as main
                     # left it, though main does convert "-$5.00".
                     "U.S$5.00", "J.C$5.00", "M.A$5.00", "SKU-US$5.00",
                     "https://x/US$5.00", "-US$5.00",
                     # Not a currency code, so only the plain "$" tail could
                     # convert: the digit-dash guard keeps it whole.
                     "It cost BUS$5.00-$6.00."):
            with self.subTest(text=text):
                self.assertEqual((text, []), verbalize_numbers(text))

    def test_season_range_needs_four_digit_years_on_both_sides(self):
        for text in ("The 737-800 season was long.", "The 2000-01 season was long.",
                     "The 2000 - 2001 season was long.", "The 2000-1999 season was long.",
                     "The 2000-20-01 season.", "ISBN 2000-2001-X season.",
                     "The 2000-2001-season.", "The 9999-10000 season was long."):
            with self.subTest(text=text):
                self.assertEqual((text, []), verbalize_numbers(text))
        # An en dash keeps its existing unconditional behaviour, and the words
        # that already followed a range still do.
        self.assertEqual("The 2000 to 2001 season was long.",
                         spoken("The 2000\u20132001 season was long."))
        self.assertEqual("The 2000 to 2001 points were many.",
                         spoken("The 2000-2001 points were many."))

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
