"""Counting text that is speakable but might be spoken wrongly.

Goal 5.1 covers characters with NO spoken form and is met - zero reach the
engine. This counts the other kind: `1999` has two readings and nothing checks
which one comes out. The categories are the ones the published Chinese TTS
front-end test sets enumerate, because those are the places a front-end quietly
differs from a reader.

Measured over the 29 shipped scripts, 98,134 speakable lines: 982 carry bare
digits, 231 a year, 188 a roman numeral, and ZERO carry an emoji - which is
goal 5.1 confirming itself from a direction that was not built to check it.
"""
import unittest

from experiments.frontend_exposure import CATEGORIES, read_scripts, scan

PATTERNS = {name: pattern for name, pattern, _ in CATEGORIES}


def hits(name, text):
    return bool(PATTERNS[name].search(text))


class CategoryTest(unittest.TestCase):
    def test_years_match_and_bare_counts_do_not(self):
        self.assertTrue(hits("year", "It was 1999 when he left."))
        self.assertTrue(hits("year", "By 2014 nobody remembered."))
        self.assertFalse(hits("year", "He counted 42 sheep."))
        self.assertFalse(hits("year", "Only 999 remained."))

    def test_ordinals_decimals_ranges_and_times(self):
        self.assertTrue(hits("ordinal", "He finished 3rd."))
        self.assertTrue(hits("ordinal", "the 21ST race"))
        self.assertTrue(hits("decimal", "0.75 metres"))
        self.assertTrue(hits("digit_range", "Chapters 12-15"))
        self.assertTrue(hits("digit_range", "pages 3 – 9"))
        self.assertTrue(hits("time_of_day", "at 7:30"))
        self.assertFalse(hits("decimal", "Mr. 5 said"))

    def test_grouped_numbers_need_a_separator(self):
        self.assertTrue(hits("grouped_number", "1,204,000 stars"))
        self.assertFalse(hits("grouped_number", "1204000 stars"))

    def test_the_roman_numeral_pattern_has_known_false_positives(self):
        """Admitted in the module rather than discovered in a result later."""
        self.assertTrue(hits("roman_numeral", "Chapter XIV begins"))
        self.assertTrue(hits("roman_numeral", "MIX"), "MIX reads as a numeral")
        self.assertTrue(hits("roman_numeral", "DID"), "DID reads as a numeral")
        self.assertFalse(hits("roman_numeral", "Chapter I begins"),
                         "a lone I would swamp the count and is excluded")

    def test_cjk_and_accented_latin_are_separated(self):
        self.assertTrue(hits("cjk", "the sign read 出口 above"))
        self.assertTrue(hits("cjk", "he shouted やめろ"))
        self.assertFalse(hits("cjk", "she said bonjour"))
        self.assertTrue(hits("non_ascii_latin", "café au lait"))
        self.assertFalse(hits("non_ascii_latin", "plain english"))

    def test_the_emoji_pattern_would_catch_one_if_it_were_there(self):
        """Zero exposure is only evidence if the detector can detect."""
        self.assertTrue(hits("emoji_or_symbol", "he smiled 😀 and left"))
        self.assertTrue(hits("emoji_or_symbol", "a ★ on the door"))
        self.assertFalse(hits("emoji_or_symbol", "nothing unusual here"))


class ScanTest(unittest.TestCase):
    def test_a_line_counts_once_per_category(self):
        tally = scan(["1999 and 2014 and 1888"])
        self.assertEqual(1, tally["year"])

    def test_a_line_can_fall_in_several_categories(self):
        tally = scan(["Chapter XIV, page 3rd, in 1999"])
        for name in ("roman_numeral", "ordinal", "year", "bare_digits"):
            self.assertEqual(1, tally[name], name)

    def test_blank_and_missing_lines_are_skipped(self):
        self.assertEqual({}, dict(scan(["", None])))


class ReadScriptsTest(unittest.TestCase):
    def test_both_script_shapes_are_read(self):
        import json, os, tempfile
        root = tempfile.mkdtemp()
        flat = os.path.join(root, "a.json")
        wrapped = os.path.join(root, "b.json")
        with open(flat, "w", encoding="utf-8") as h:
            json.dump([{"text": "one"}, {"text": ""}], h)
        with open(wrapped, "w", encoding="utf-8") as h:
            json.dump({"entries": [{"text": "two"}]}, h)
        self.assertEqual(["one", "two"], read_scripts([flat, wrapped]))

    def test_an_unreadable_file_is_skipped_not_fatal(self):
        import os, tempfile
        bad = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(bad, "w", encoding="utf-8") as h:
            h.write("{ not json")
        self.assertEqual([], read_scripts([bad]))
