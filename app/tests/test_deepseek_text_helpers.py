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
    """origin/main's unbounded extract_json_object, used as the differential
    reference: the budgeted version must return the same answer below budget.
    It calls the shared extract_balanced, which is byte-identical to main."""
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

    def test_brace_storms_are_refused_within_the_work_budget(self):
        # Work (characters scanned and parsed) is bounded, not wall-clock and
        # not a candidate cap, so a reply of stray braces cannot rescan the
        # tail from every earlier brace. A small explicit budget keeps this
        # fast; the constant is sized for real replies separately. The last
        # increment before the check can be 2x the text, so work overshoots by
        # at most that much.
        budget = 200_000
        for text in ('{' * 100000,
                     '{' * 10000 + '}',
                     '{' * 2000 + '}' * 2000):
            with self.subTest(length=len(text)):
                result, work = utils_module._extract_json_object_with_work(
                    text, budget)
                self.assertIsNone(result)
                self.assertLessEqual(work, budget + 2 * len(text))

    def test_unclosed_brace_run_does_not_scan_the_tail_even_once(self):
        # With no closing brace anywhere, nothing can balance; the rfind skips
        # all candidates and no scan is attempted.
        with patch.object(utils_module, 'extract_balanced',
                          wraps=utils_module.extract_balanced) as scan:
            self.assertIsNone(extract_json_object('{' * 100000))
        scan.assert_not_called()

    def test_recursion_error_from_the_whole_text_parse_refuses_without_scanning(self):
        # Inject RecursionError so the test does not depend on the
        # interpreter's own JSON recursion limit (3.12+ parses much deeper).
        with patch.object(utils_module.json, 'loads',
                          side_effect=RecursionError), \
                patch.object(utils_module, 'extract_balanced',
                             wraps=utils_module.extract_balanced) as scan:
            self.assertIsNone(extract_json_object('{"a": 1}'))
        scan.assert_not_called()

    def test_recursion_error_in_a_candidate_span_refuses_the_whole_reply(self):
        # A deep junk object must not be skipped over to return a later,
        # shallower object (or a truncated inner one): the reply is refused.
        # The depth failure is injected so the test is version-independent.
        text = 'prefix {"a": 1} {"ok": 1}'

        def loads(value, *args, **kwargs):
            if value == text:
                raise json.JSONDecodeError("Expecting value", value, 0)
            raise RecursionError("depth limit")

        with patch.object(utils_module.json, 'loads', side_effect=loads), \
                patch.object(utils_module, 'extract_balanced',
                             wraps=utils_module.extract_balanced) as scan:
            self.assertIsNone(extract_json_object(text))
        self.assertEqual(1, scan.call_count)

    def test_valid_object_after_more_than_64_stray_open_braces_is_found(self):
        text = '{' * 100 + ' prior text {"ok": 1}'
        self.assertEqual({'ok': 1}, extract_json_object(text))

    def test_valid_object_after_strays_in_a_large_reply_matches_the_reference(self):
        # 200 strays in a ~20 KB reply is below the real work budget, so the
        # answer must be exactly what the unbounded reference returns.
        text = '{' * 200 + 'x' * 20000 + ' {"ok": 1}'
        result, work = utils_module._extract_json_object_with_work(
            text, utils_module.EXTRACT_JSON_OBJECT_WORK_BUDGET)
        self.assertEqual({'ok': 1}, result)
        self.assertLessEqual(work, utils_module.EXTRACT_JSON_OBJECT_WORK_BUDGET)
        self.assertEqual(reference_extract_json_object(text), result)

    def test_deeply_nested_json_shapes_are_refused_not_partially_parsed(self):
        self.assertIsNone(extract_json_object('[' * 5000 + ']' * 5000))
        self.assertIsNone(extract_json_object('[' * 5000 + ']' * 4999))
        # Inject the depth failure rather than relying on the interpreter's
        # JSON limit, which 3.12+ raises high enough to parse this object and
        # return it (base would have parsed it too, so the refusal is only
        # guaranteed when the parser actually raises).
        deep = '{"a":' * 5000 + '1' + '}' * 5000
        with patch.object(utils_module.json, 'loads',
                          side_effect=RecursionError):
            self.assertIsNone(extract_json_object(deep))

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
