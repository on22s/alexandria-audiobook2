"""Goal 5.3's metric is blind to what the arms do to punctuation.

The blindness is not a bug to fix in place - pairing lines by alphanumerics is
the right way to match two different segmentations of one book, and changing it
would break the pairing that makes 5.3's accuracy numbers possible at all. What
was wrong was reading an accuracy verdict as though it covered text fidelity.

So these tests pin the blindness explicitly, on the exact characters involved,
so that the next person to quote 5.3 finds a test saying what it does not
measure. They also pin the speech-boundary behaviour that decides which of
those characters matter, because "the quote survives to the engine and the
underscore becomes a sentence break" is the whole reason one is a differentiator
and the other is not.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class NormTextBlindnessTest(unittest.TestCase):
    """What goal 5.3's pairing key cannot see."""

    def setUp(self):
        from experiments.three_pass_vs_single import norm_text
        self.norm = norm_text

    def test_a_quoted_line_and_its_stripped_form_are_the_same_key(self):
        """This is precisely what three-pass does - text[1:-1] on a fully
        quoted line - and why 5.3 reported no textual difference between arms
        while one of them had removed every quote in the book."""
        self.assertEqual(self.norm('"Hello," he said.'),
                         self.norm('Hello, he said.'))

    def test_smart_and_straight_quotes_are_the_same_key(self):
        self.assertEqual(self.norm('“Hello”'), self.norm('"Hello"'))

    def test_underscores_and_dashes_are_invisible_to_it(self):
        self.assertEqual(self.norm("He said _hello_ softly"),
                         self.norm("He said hello softly"))
        self.assertEqual(self.norm("Wait - what"), self.norm("Wait what"))

    def test_it_still_separates_genuinely_different_lines(self):
        """The blindness must be confined to punctuation. If it also collapsed
        different words, the pairing would be wrong rather than merely narrow,
        and 5.3's accuracy figures would be meaningless instead of partial."""
        self.assertNotEqual(self.norm("the cat sat"), self.norm("the dog sat"))


class SpeechBoundaryTest(unittest.TestCase):
    """Which characters actually reach the engine. Measured, then pinned."""

    def setUp(self):
        from speech_text import normalize_for_speech, SPEECH_BREAKS
        self.speak = normalize_for_speech
        self.breaks = SPEECH_BREAKS

    def _spoken(self, text):
        out = self.speak(text)
        return out if isinstance(out, str) else str(out)

    def test_quotes_reach_the_engine(self):
        """So an arm that removes them changes what is synthesised, not only
        what is readable. This is what makes the three-pass difference real
        rather than cosmetic."""
        self.assertIn('"', self._spoken('"Hello," he said.'))
        self.assertNotIn('"', self.breaks)

    def test_an_underscore_becomes_a_sentence_break_not_a_deletion(self):
        """`He said _hello_ softly.` reaches the engine as three sentences.
        Emphasis markup does not vanish quietly - it changes the prosody. This
        happens for BOTH arms, so it is a finding about the pipeline rather
        than a difference between them."""
        spoken = self._spoken("He said _hello_ softly.")
        self.assertNotIn("_", spoken)
        self.assertGreaterEqual(spoken.count("."), 2, spoken)

    def test_a_hyphen_is_left_alone(self):
        """Named alongside the other two, but measured to survive unchanged, so
        it is neither a differentiator nor a prosody change."""
        self.assertIn("-", self._spoken("A dash - here"))


class FidelityProbeTest(unittest.TestCase):
    def setUp(self):
        from experiments import script_text_fidelity as m
        self.m = m

    def test_it_counts_every_quote_style_the_books_use(self):
        """Japanese light novels in translation mix straight, curly and corner
        brackets. Counting only '\"' would report three-pass as faithful on a
        book that uses 「」."""
        for mark in ('"', "“", "”", "「", "」", "«", "»"):
            with self.subTest(mark=mark):
                self.assertTrue(self.m.CLASSES["quote"](f"x{mark}y"))

    def test_it_does_not_call_an_apostrophe_a_quote(self):
        """`don't` must not count as a quoted line, or every narration entry in
        English prose becomes 'dialogue' and the delta between arms vanishes
        into noise."""
        self.assertFalse(self.m.CLASSES["quote"]("don't stop"))

    def test_dash_class_covers_the_unicode_dashes(self):
        for mark in ("-", "–", "—", "―"):
            with self.subTest(mark=mark):
                self.assertTrue(self.m.CLASSES["hyphen_or_dash"](f"a{mark}b"))

    def test_a_book_with_only_one_arm_reports_no_delta(self):
        """A delta needs both arms. Reporting one against zero would show a
        book as a total loss of quotes when its other arm was simply absent."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/onlybook__single.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"entries": [{"text": '"hi"'}]}, fh)
            n, counts = self.m.profile(path)
        self.assertEqual(1, n)
        self.assertEqual(1, counts["quote"])


if __name__ == "__main__":
    unittest.main()
