"""Hand-known evidence locations and bounded scans on actual normalizers."""
import unittest
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

    def test_caption_locations_use_pre_removal_coordinates_after_known_repairs(self):
        source = 'plain ' * 300 + '\nсаге\nIllustration from Volume 1, coloring by Artist (source)\n\nstory\nIllustration from Volume 2, coloring by Artist (source)\nend'
        actual, changes = normalization.normalize_known_source_corruptions(source)
        self.assertEqual(3, len(changes))
        self.assertEqual([2, 3, 6], [change['line'] for change in changes])
        self.assertEqual([1, 1, 1], [change['column'] for change in changes])
        self.assertNotIn('Illustration from', actual)
        for change in changes:
            self.assertEqual(change['before'], source[change['offset']:change['offset'] + len(change['before'])])
