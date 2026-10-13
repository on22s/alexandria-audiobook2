"""Recovering a quote's neighbours from the source, and refusing to guess.

The light-novel fixtures carry no `prev_context`/`next_context`, so every
context-reading experiment on those four books goes through this alignment.
Two refusals matter more than the matching does: a line that appears twice
must not silently take the first occurrence's neighbours, and a line too short
to be unique must not be located at all. Light-novel dialogue is full of both -
grimgar03 alone has 92 gold lines under the length floor.
"""
import tempfile
import unittest
from pathlib import Path

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

    def test_lowercase_expansion_keeps_exact_original_offsets(self):
        source = "İ Alice said hello"
        normalised, offsets = build_index(source)
        self.assertEqual("ialicesaidhello", normalised)
        self.assertEqual(normalize(source), normalised)
        self.assertEqual([0, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 17],
                         offsets)

    def test_expansion_before_inside_and_after_quote_preserves_context_slices(self):
        for before, quote, after in (
                ("İ prefix: ", "Alice said hello", "; next"),
                ("prefix: ", "Alİce said hello", "; next"),
                ("prefix: ", "Alice said hellİ", "; next"),
                ("prefix: ", "Alice said hello", "; next İ")):
            with self.subTest(before=before, quote=quote, after=after):
                source = before + quote + after
                normalised, offsets = build_index(source)
                self.assertEqual(normalize(source), normalised)
                self.assertEqual(len(normalised), len(offsets))
                self.assertEqual((before[-7:], after[:7], "located"),
                                 locate(quote, normalised, offsets, source, window=7))

    def test_expansion_does_not_resolve_a_repeated_line(self):
        source = "İ Alice said hello. Alice said hello."
        normalised, offsets = build_index(source)
        self.assertEqual((None, None, "ambiguous"),
                         locate("Alice said hello", normalised, offsets, source))

    def test_matching_retains_existing_ascii_extraction_policy(self):
        source = "CAFÉ Straße İ １２ Привет"
        normalised, offsets = build_index(source)
        self.assertEqual("cafstraei", normalize(source))
        self.assertEqual(normalize(source), normalised)
        self.assertEqual(len(normalised), len(offsets))

    def test_elson_source_loader_recovers_context_after_expansion(self):
        from experiments.elson_trigram import contexts_for, load_sources
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.txt"
            path.write_text("İ Alice said hello", encoding="utf-8")
            sources = load_sources([f"synthetic={path}"])
        self.assertEqual(("İ ", "", "located"),
                         contexts_for({"line": "Alice said hello"},
                                      "synthetic", sources))
