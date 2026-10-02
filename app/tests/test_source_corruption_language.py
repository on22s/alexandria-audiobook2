"""OCR guesses must respect the source's writing system and word boundaries."""
import unittest

from generate_script import get_preprocessed_source
from source_normalization import normalize_known_source_corruptions, normalize_homoglyph_words


class SourceCorruptionLanguageTests(unittest.TestCase):
    def test_genuine_russian_survives_real_generator_preprocessing(self):
        text = 'Горячий пар поднимался над водой. Пар окутал парк и паровоз.'
        self.assertEqual((text, []), normalize_known_source_corruptions(text))
        normalized, report = get_preprocessed_source(text, strip_front_matter=False)
        self.assertEqual(text, normalized)
        self.assertEqual([], report['source_normalizations'])

    def test_other_writing_systems_do_not_count_as_latin_evidence(self):
        for padding in ('中文故事繼續敘述' * 300, 'Ελληνική αφήγηση' * 300, 'العربية' * 300):
            text = padding + '\nпар саге Subаru'
            for normalizer in (normalize_known_source_corruptions, normalize_homoglyph_words):
                with self.subTest(normalizer=normalizer.__name__, padding=padding[:12]):
                    self.assertEqual((text, []), normalizer(text))

    def test_bilingual_and_short_ambiguous_texts_remain_unchanged(self):
        for text in ('Take пар now.', 'пар', '', 'English story. Горячий пар. ' * 30):
            with self.subTest(text=text[:20]):
                self.assertEqual((text, []), normalize_known_source_corruptions(text))

    def test_whole_words_only_and_original_location_evidence(self):
        padding = 'The narrator continued telling the story calmly. ' * 400
        text = padding + '\nсагевый xпар парx паровоз park_пар саге. Пар!'
        normalized, changes = normalize_known_source_corruptions(text)
        self.assertEqual(padding + '\nсагевый xпар парx паровоз park_пар care. Nap!', normalized)
        self.assertEqual(['саге', 'Пар'], [change['before'] for change in changes])
        for change in changes:
            self.assertEqual(change['before'], text[change['offset']:change['offset'] + len(change['before'])])
            self.assertEqual(2, change['line'])
            self.assertTrue(text.split('\n')[1][change['column'] - 1:].startswith(change['before']))

    def test_accented_latin_source_remains_eligible_for_repair(self):
        padding = 'Émilie racontait une scène où François était présent. ' * 40
        text = padding + '\nсаге пар'
        normalized, changes = normalize_known_source_corruptions(text)
        self.assertEqual(padding + '\ncare nap', normalized)
        self.assertEqual(2, len(changes))

    def test_caption_rule_remains_independent_of_script_guard(self):
        text = 'Горячий пар. Illustration from Volume 1, coloring by Artist (source) Продолжение.'
        normalized, changes = normalize_known_source_corruptions(text)
        self.assertEqual('Горячий пар. Продолжение.', normalized)
        self.assertEqual(['illustration_caption'], [c['rule'] for c in changes])
