"""Source-backed transformations and malformed-prose recovery stay guarded."""
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import utils as utils_module
from apostrophe_repair import restore_stripped_apostrophes
from chunk_quality import validate_chunk_quality
from utils import extract_json_object, is_path_inside
from tests.test_support import can_symlink


def reference_extract_json_object(text):
    """The pre-fix scan, kept to prove the faster scan is results-identical."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    search_from = 0
    while True:
        start = text.find('{', search_from)
        if start == -1:
            return None
        span = utils_module.extract_balanced(text, '{', '}', search_from=start)
        if span is None:
            search_from = start + 1
            continue
        try:
            return json.loads(span)
        except json.JSONDecodeError:
            search_from = start + 1

_SYMLINK_SKIP_REASON = 'symlink creation not permitted (WinError 1314 / host refusal)'


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

    def test_unclosed_brace_run_does_not_rescan_from_every_open_brace(self):
        text = '{' * 2000
        with patch.object(utils_module, 'extract_balanced',
                          wraps=utils_module.extract_balanced) as balanced:
            self.assertIsNone(extract_json_object(text))
        self.assertLessEqual(balanced.call_count, 1)

    def test_valid_object_after_more_than_64_stray_open_braces_is_found(self):
        text = '{' * 100 + ' prior text {"ok": 1}'
        self.assertEqual({'ok': 1}, extract_json_object(text))

    def test_deeply_nested_json_shapes_do_not_escape_recursion_error(self):
        self.assertIsNone(extract_json_object('[' * 5000 + ']' * 5000))
        self.assertIsNone(extract_json_object('[' * 5000 + ']' * 4999))
        nested = extract_json_object('{"a":' * 5000 + '1' + '}' * 5000)
        self.assertIsInstance(nested, dict)

    def test_faster_scan_matches_the_reference_on_randomized_text(self):
        rng = random.Random(20261124)
        alphabet = '{}[]"\\ ab1:,.-_'
        for _ in range(1000):
            text = ''.join(rng.choice(alphabet)
                           for _ in range(rng.randint(0, 80)))
            try:
                expected = reference_extract_json_object(text)
            except RecursionError:
                continue
            with self.subTest(text=text):
                self.assertEqual(expected, extract_json_object(text))

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
            with self.subTest('symlink escape'):
                if not can_symlink():
                    self.skipTest(_SYMLINK_SKIP_REASON)
                outside = Path(tmp) / 'outside'
                outside.mkdir()
                (base / 'link').symlink_to(outside, target_is_directory=True)
                self.assertFalse(is_path_inside(base / 'link' / 'child', base))
