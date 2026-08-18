"""The spoken-line detector, checked against lines whose answer is known.

Every fixture below is a REAL entry from the generated mushoku18 script. The
first attempt at this measurement counted any entry containing a quotation mark
and reported 74% of dialogue left with the narrator; three of the four cases it
was shown were narration that legitimately contains a quote. A metric that
fails the examples you can already judge is not measuring what you asked
(Rule 21), and the rejects below are the ones that fooled it.
"""
import unittest

from experiments.measure_dialogue_attribution import (classify, is_spoken_line,
                                                      measurable, quote_coverage)

# Real entries that ARE lines of dialogue: the quote is the whole entry.
SPOKEN = [
    '"What? I didn’t cry like an idiot!"',
    '"First thing’s first, you. Rudeus Greyrat."',
    '"Everybody, please come here."',
    '"Tina Thrush?"',
    '"......?"',
    '“It’s better to regret doing it, than to regret not doing it.”',
]

# Real entries that are NARRATION containing a quote, and must NOT count.
NARRATION = [
    'Do they not know the meaning of the word "Incognito"?',
    'After hearing my suggestion, Ariel was posing thoughtfully saying "Hmm".',
    'It was a face that seemed to say: "I can do this much if I want to, you know."',
    'She said, “You’ll be fine if you leave it to Rudeus.” For Eris-sama, '
    'staying silent and hearing all you had to say must have been difficult.',
]


class SpokenLineTest(unittest.TestCase):
    def test_real_dialogue_is_detected(self):
        missed = [t for t in SPOKEN if not is_spoken_line(t)]
        self.assertEqual([], missed)

    def test_narration_holding_a_quote_is_rejected(self):
        """THE CASES THAT FOOLED THE FIRST VERSION."""
        wrong = [t for t in NARRATION if is_spoken_line(t)]
        self.assertEqual([], wrong)

    def test_the_naive_rule_would_fail_these(self):
        """Keeps the fixtures honest: if they stop discriminating, this fails.

        The retired rule was "contains a quotation mark", and it accepted every
        line in NARRATION - which is exactly how 74% got quoted at someone.
        """
        naive = lambda t: '"' in t or "“" in t
        self.assertTrue(all(naive(t) for t in NARRATION))

    def test_an_entry_with_no_quote_is_not_a_spoken_line(self):
        self.assertFalse(is_spoken_line("Ariel nodded slowly and said nothing."))
        self.assertEqual(0.0, quote_coverage(""))

    def test_coverage_is_a_share_not_a_count(self):
        # A long narration with one short quote must score low even though the
        # quote is present; a bare line of dialogue must score ~1.
        self.assertLess(quote_coverage(NARRATION[0]), 0.5)
        self.assertGreater(quote_coverage(SPOKEN[1]), 0.9)


class ClassifyTest(unittest.TestCase):
    def test_it_separates_narrator_attributed_dialogue(self):
        entries = ([{"speaker": "NARRATOR", "text": t} for t in SPOKEN[:4]] +
                   [{"speaker": "ERIS", "text": t} for t in SPOKEN[4:]] +
                   [{"speaker": "NARRATOR", "text": t} for t in NARRATION])
        out = classify(entries)
        self.assertEqual(6, out["spoken_lines"])   # narration excluded
        self.assertEqual(4, out["left_with_narrator"])

    def test_curly_quoted_lines_are_reported_separately(self):
        """First-person thought belongs to the narrator, so it cannot be
        counted as a misattribution without a reader who knows the book."""
        entries = [{"speaker": "NARRATOR", "text": '“So that is how it is.”'},
                   {"speaker": "NARRATOR", "text": '"Get down!"'}]
        out = classify(entries)
        self.assertEqual(2, out["left_with_narrator"])
        self.assertEqual(1, out["straight_quoted_only"]["left_with_narrator"])


class MeasurabilityTest(unittest.TestCase):
    """A book that does not quote its dialogue must be refused, not scored.

    arc4_volume10wn writes dialogue with no quotation marks at all, so the
    detector found 22 spoken lines in 6,173 entries and reported 59.1% - a
    number about the instrument, not the book.
    """

    def test_an_unquoted_book_is_refused(self):
        entries = [{"speaker": "SUBARU", "text": "Say, Petra, isn't this kinda close?"},
                   {"speaker": "PETRA", "text": "No? Is there some problem, Subaru?"}] * 50
        ok, why = measurable(entries)
        self.assertFalse(ok)
        self.assertIn("quotation marks", why)

    def test_a_quoting_book_is_measured(self):
        entries = ([{"speaker": "ERIS", "text": '"Get down!"'}] * 10 +
                   [{"speaker": "NARRATOR", "text": "She moved."}] * 40)
        ok, why = measurable(entries)
        self.assertTrue(ok, why)

    def test_no_entries_is_refused_rather_than_divided_by_zero(self):
        self.assertFalse(measurable([])[0])


class ApostropheTest(unittest.TestCase):
    """An apostrophe is not a quotation mark.

    The first pattern accepted `'[^']{4,}'`, so a line like "Feel like a dad.
    My supposedly-absent paternal instincts, now, are bubbling up" - two
    apostrophes, four characters apart - counted as quoted. arc4_volume10wn
    has no quotation marks anywhere, and that alone lifted it over the
    measurability floor and produced a confident 59.1% about nothing.
    """

    APOSTROPHES = [
        "Feel like a dad. My supposedly-absent paternal instincts, now, "
        "are bubbling up from within me.",
        "Say… Petra, isn't this kinda close?",
        "I don't know what's wrong with them, honestly.",
    ]

    def test_apostrophes_do_not_make_a_line_look_quoted(self):
        for line in self.APOSTROPHES:
            self.assertEqual(0.0, quote_coverage(line), line)
            self.assertFalse(is_spoken_line(line), line)

    def test_a_book_written_this_way_is_refused(self):
        entries = [{"speaker": "SUBARU", "text": t} for t in self.APOSTROPHES] * 40
        ok, why = measurable(entries)
        self.assertFalse(ok, why)
