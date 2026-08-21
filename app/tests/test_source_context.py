"""Recovering a quote's neighbours from the source, and refusing to guess.

The light-novel fixtures carry no `prev_context`/`next_context`, so every
context-reading experiment on those four books goes through this alignment.
Two refusals matter more than the matching does: a line that appears twice
must not silently take the first occurrence's neighbours, and a line too short
to be unique must not be located at all. Light-novel dialogue is full of both -
grimgar03 alone has 92 gold lines under the length floor.
"""
import unittest

from experiments.source_context import build_index, locate, normalize

SOURCE = (
    'The room was quiet.\n\n'
    '"I have nothing further to add," said Mr. Darcy, turning away.\n\n'
    'She watched him go.\n\n'
    '"That is quite impossible."\n\n'
    'He shook his head.\n\n'
    '"That is quite impossible."\n'
)


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.normalised, self.offsets = build_index(SOURCE)

    def _locate(self, line, **kw):
        return locate(line, self.normalised, self.offsets, SOURCE, **kw)

    def test_a_unique_line_yields_its_neighbours(self):
        prev, nxt, status = self._locate("I have nothing further to add,")
        self.assertEqual("located", status)
        self.assertTrue(nxt.startswith('," said Mr. Darcy'), nxt[:40])
        self.assertIn("The room was quiet.", prev)

    def test_a_repeated_line_is_ambiguous_not_the_first_hit(self):
        """Picking one attaches the wrong neighbours to a real gold row."""
        line = "That is quite impossible."
        self.assertGreaterEqual(len(normalize(line)), 12,
                                "the fixture must clear the length floor, or "
                                "this passes for the wrong reason")
        prev, nxt, status = self._locate(line)
        self.assertEqual("ambiguous", status)
        self.assertIsNone(prev)
        self.assertIsNone(nxt)

    def test_the_same_line_appearing_once_does_locate(self):
        """The discriminating half: ambiguity must come from repetition."""
        source = SOURCE.replace('He shook his head.\n\n'
                                '"That is quite impossible."\n', "")
        normalised, offsets = build_index(source)
        self.assertEqual("located", locate("That is quite impossible.",
                                           normalised, offsets, source)[2])

    def test_a_short_line_is_refused_rather_than_matched_everywhere(self):
        self.assertEqual("too_short", self._locate("Ah!")[2])
        self.assertEqual((None, None), self._locate("Ah!")[:2])

    def test_a_line_that_is_not_in_the_book_is_not_found(self):
        self.assertEqual("not_found",
                         self._locate("This sentence is absent from the source")[2])

    def test_matching_ignores_quote_convention_and_wrapping(self):
        """Generation re-inserts quote marks and translators mix " with 『』."""
        prev, nxt, status = self._locate('『I have nothing\nfurther to add』')
        self.assertEqual("located", status)

    def test_normalize_keeps_only_alphanumerics(self):
        self.assertEqual("ihavenothing", normalize('"I have nothing!"'))
