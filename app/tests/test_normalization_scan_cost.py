"""Hand-known evidence locations and bounded scans on actual normalizers."""
import unittest
from unittest.mock import patch
import source_normalization as normalization


class CountedText(str):
    def __new__(cls, text):
        value = super().__new__(cls, text)
        value.scans = 0
        return value
    def count(self, *args):
        self.scans += 1
        return super().count(*args)
    def rfind(self, *args):
        self.scans += 1
        return super().rfind(*args)


class NormalizationScanCostTests(unittest.TestCase):
    def test_known_edits_keep_each_original_line_column_without_repeated_prefix_scans(self):
        source = CountedText(('plain ' * 200 + 'саге\n') * 100)
        actual, changes = normalization.normalize_known_source_corruptions(source)
        self.assertEqual(str(source).replace('саге', 'care'), actual)
        self.assertEqual(100, len(changes))
        for index, change in enumerate(changes):
            self.assertEqual((index + 1, 1201), (change['line'], change['column']))
            self.assertEqual('саге', source[change['offset']:change['offset'] + 4])
        self.assertLessEqual(source.scans, 2, 'Evidence repeatedly scans the document prefix')

    def test_homoglyph_locations_preserve_unicode_offsets(self):
        source = CountedText(('café ' * 200 + 'cаre\n') * 100)
        actual, changes = normalization.normalize_homoglyph_words(source)
        self.assertEqual(str(source).replace('cаre', 'care'), actual)
        self.assertEqual(100, len(changes))
        for index, change in enumerate(changes):
            self.assertEqual((index + 1, 1001), (change['line'], change['column']))
            self.assertEqual('cаre', source[change['offset']:change['offset'] + 4])
        self.assertLessEqual(source.scans, 2)

    def test_long_replacement_run_is_scanned_once_and_keeps_known_inference(self):
        class CountedChars(list):
            reads = 0
            def __getitem__(self, key):
                type(self).reads += 1
                return super().__getitem__(key)
        length = 500
        source = '\n' + '�' * length + '\n'
        with patch.object(normalization, 'list', CountedChars, create=True):
            actual, changes = normalization.repair_lossy_replacements(source)
        self.assertEqual('\n“' + '…' * (length - 2) + '”\n', actual)
        self.assertEqual(list(range(1, length + 1)), [change['offset'] for change in changes])
        self.assertLess(CountedChars.reads, 20 * length, 'Each position rescans its remaining run')

    def test_caption_locations_use_pre_removal_coordinates_after_known_repairs(self):
        source = 'plain ' * 300 + '\nсаге\nIllustration from Volume 1, coloring by Artist (source)\n\nstory\nIllustration from Volume 2, coloring by Artist (source)\nend'
        actual, changes = normalization.normalize_known_source_corruptions(source)
        self.assertEqual(3, len(changes))
        self.assertEqual([2, 3, 6], [change['line'] for change in changes])
        self.assertEqual([1, 1, 1], [change['column'] for change in changes])
        self.assertNotIn('Illustration from', actual)
        for change in changes:
            self.assertEqual(change['before'], source[change['offset']:change['offset'] + len(change['before'])])
