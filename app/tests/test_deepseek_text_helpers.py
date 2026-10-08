"""Source-backed transformations and malformed-prose recovery stay guarded."""
import os
import tempfile
import unittest
from pathlib import Path

from apostrophe_repair import restore_stripped_apostrophes
from chunk_quality import validate_chunk_quality
from utils import extract_json_object, is_path_inside


class DeepSeekTextHelperTests(unittest.TestCase):
    def test_uppercase_suffixes_preserve_case_and_damage_gate(self):
        source = 'DON T GO. HE S SURE WE RE READY. ONCE RE-ENTERED.'
        repaired, changes = restore_stripped_apostrophes(source)
        self.assertEqual("DON'T GO. HE'S SURE WE'RE READY. ONCE RE-ENTERED.", repaired)
        self.assertEqual(len(source), len(repaired))
        self.assertEqual(["DON'T", "HE'S", "WE'RE"], [row['after'] for row in changes])
        healthy = "It's fine. " * 100 + 'DON T GO. HE S SURE.'
        self.assertEqual((healthy, []), restore_stripped_apostrophes(healthy))

    def test_latin_accent_case_is_source_backed_but_new_letters_fail(self):
        for source, output in [('Café is open.', 'CAFÉ IS OPEN.'),
                               ('CAFÉ IS OPEN.', 'Café is open.'),
                               ('Cafe\u0301 is open.', 'CAFÉ IS OPEN.')]:
            with self.subTest(source=source):
                report = validate_chunk_quality(source, [{'text': output, 'speaker': 'NARRATOR',
                                                         'instruct': 'Read naturally.'}])
                self.assertNotIn('unsupported_unicode_character',
                                 {row['code'] for row in report['findings']})
        for output in ('Cafë is open.', 'Cafλ is open.'):
            report = validate_chunk_quality('Café is open.', [{'text': output, 'speaker': 'NARRATOR',
                                                              'instruct': 'Read naturally.'}])
            self.assertIn('unsupported_unicode_character',
                          {row['code'] for row in report['findings']})

    def test_unmatched_prose_brace_does_not_hide_later_object(self):
        self.assertEqual({'message': 'quoted } brace', 'nested': {'ok': True}},
                         extract_json_object('Notes { unfinished\n' +
                                             '{"message":"quoted } brace","nested":{"ok":true}}'))
        self.assertEqual({'ok': 1}, extract_json_object('Notes { stray "quote\n{"ok":1}'))
        for value in ('Notes { unfinished', '{"ok":', 'not JSON', '[1, 2]'):
            with self.subTest(value=value):
                self.assertIsNone(extract_json_object(value))

    def test_filesystem_root_contains_child_and_sibling_prefix_stays_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).anchor
            self.assertTrue(is_path_inside(tmp, root))
            self.assertTrue(is_path_inside(root, root))
            base = Path(tmp) / 'base'
            base.mkdir()
            self.assertTrue(is_path_inside(base / 'child', base))
            self.assertFalse(is_path_inside(Path(tmp) / 'base-other', base))
            self.assertFalse(is_path_inside(base / '..' / 'escape', base))
            if hasattr(os, 'symlink'):
                outside = Path(tmp) / 'outside'
                outside.mkdir()
                (base / 'link').symlink_to(outside, target_is_directory=True)
                self.assertFalse(is_path_inside(base / 'link' / 'child', base))
