"""Matching a script back to its source, when generation altered the text.

The matcher pairs a generated script with the book it came from by content,
because filenames do not map ("Arc 1 - Volume 1.json" against 57 candidate
sources) and no manifest records the pairing.

ITS FIRST VERSION TESTED EXACT SUBSTRINGS and refused all 29 scripts at 0.0
containment. Correctly refusing - but for the wrong reason: generation removes
the outermost quotes, so a line the model produced is not a substring of the
source that produced it. The matcher had walked into the very defect it was
written to repair. These tests pin the fix and the refusals.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class LooseMatchTest(unittest.TestCase):
    def setUp(self):
        from experiments import retrofit_dialogue_map as m
        self.m = m

    def test_a_stripped_line_matches_its_quoted_source(self):
        """The case that refused 29 of 29."""
        source = 'He paused. "Hello there, friend," she said warmly.'
        stripped = "Hello there, friend,"
        self.assertIn(self.m.loose(stripped), self.m.loose(source))

    def test_curly_and_corner_quotes_are_stripped_too(self):
        for pair in (("“", "”"), ("「", "」")):
            with self.subTest(pair=pair):
                source = f"He paused. {pair[0]}Hello there{pair[1]} she said."
                self.assertIn(self.m.loose("Hello there"), self.m.loose(source))

    def test_whitespace_differences_do_not_break_a_match(self):
        self.assertEqual(self.m.loose("a  b\n c"), self.m.loose("a b c"))

    def test_an_apostrophe_inside_a_word_is_preserved(self):
        """Stripping ' as if it were a quote would turn "don't" into "dont"
        on one side only, and every contraction would stop matching."""
        self.assertIn("don't", self.m.loose("She said don't go"))


class SourceSelectionTest(unittest.TestCase):
    """A wrongly paired book produces a confident map of the wrong novel,
    which is worse than no map at all."""

    def setUp(self):
        from experiments import retrofit_dialogue_map as m
        self.m = m
        import random
        self.rng = random.Random(0)

    def _entries(self, lines):
        return [{"text": t} for t in lines]

    def test_the_right_source_wins_and_is_reported(self):
        lines = [f"The quick brown fox jumped over the lazy dog number {i}"
                 for i in range(12)]
        right = " ".join(lines)
        wrong = "Something else entirely, with none of those sentences in it. " * 12
        sources = {"right.txt": right, "wrong.txt": wrong}
        self.m.loose_sources.clear()
        self.m.loose_sources.update({k: self.m.loose(v) for k, v in sources.items()})
        path, containment, _margin = self.m.pick_source(
            self._entries(lines), sources, self.rng)
        self.assertEqual("right.txt", path)
        self.assertGreaterEqual(containment, self.m.MIN_CONTAINMENT)

    def test_two_equally_good_sources_are_refused(self):
        """Duplicate texts under different names are common here - the same
        book appears in several results directories. Picking either would be a
        coin flip presented as a match."""
        lines = [f"An identical sentence appears in both copies, number {i}"
                 for i in range(12)]
        text = " ".join(lines)
        sources = {"copy_a.txt": text, "copy_b.txt": text}
        self.m.loose_sources.clear()
        self.m.loose_sources.update({k: self.m.loose(v) for k, v in sources.items()})
        path, _c, margin = self.m.pick_source(self._entries(lines), sources, self.rng)
        self.assertIsNone(path, f"margin was {margin}")

    def test_a_book_with_no_matching_source_is_refused(self):
        lines = [f"Nothing here appears in any candidate source, line {i}"
                 for i in range(12)]
        sources = {"other.txt": "Completely unrelated prose. " * 40}
        self.m.loose_sources.clear()
        self.m.loose_sources.update({k: self.m.loose(v) for k, v in sources.items()})
        path, containment, _m = self.m.pick_source(
            self._entries(lines), sources, self.rng)
        self.assertIsNone(path)
        self.assertLess(containment, self.m.MIN_CONTAINMENT)

    def test_short_lines_are_not_used_as_probes(self):
        """"Yes." appears in every novel ever written and would match all
        candidates equally, turning selection into a coin flip."""
        probes = self.m.samples_of(self._entries(["Yes.", "No.", "Ah."]),
                                   self.rng)
        self.assertEqual([], probes)


if __name__ == "__main__":
    unittest.main()
