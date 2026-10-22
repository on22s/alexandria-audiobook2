"""Scope ranges require the complete current ordered source, never a fuzzy heading."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from project_scope_source import get_source_scope, get_scope_source_snapshot


class SourceScopeTests(unittest.TestCase):
    def test_bounded_snapshot_decodes_native_source_and_preserves_raw_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()/'source.txt'
            raw = b'First synthetic line.\r\nSecond \x93quoted\x94 line.\r'
            path.write_bytes(raw)
            state = {'input_file_path': str(path)}; before = copy.deepcopy(state)
            proof, source = get_scope_source_snapshot(state)
            self.assertEqual('First synthetic line.\nSecond “quoted” line.\n', source)
            self.assertEqual('cp1252', proof['encoding'])
            self.assertIsNone(proof['strip_front_matter'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['sha256'])
            self.assertEqual(raw, path.read_bytes()); self.assertEqual(before, state)
            changed = raw.replace(b'First', b'Third'); path.write_bytes(changed)
            updated, _ = get_scope_source_snapshot(state)
            self.assertNotEqual(proof['sha256'], updated['sha256'])

    def test_saved_preparation_removes_native_publisher_matter_before_scope_comparison(self):
        from tests.test_publisher_matter import StripPublisherMatterTest
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()/'source.txt'
            raw = (StripPublisherMatterTest.FRONT + StripPublisherMatterTest.STORY).encode()
            path.write_bytes(raw)
            state = {'input_file_path': str(path), 'script_generation_input_file': str(path),
                     'script_generation_options': {'strip_front_matter': False}}
            proof, prepared = get_scope_source_snapshot(state)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['sha256'])
            self.assertFalse(proof['strip_front_matter'])
            self.assertNotIn(StripPublisherMatterTest.FRONT.strip(), prepared)
            words = prepared.split(); split = len(words)//2
            entries = [{'text': ' '.join(words[:split])}, {'text': ' '.join(words[split:])}]
            scope = get_source_scope(prepared, entries, 1)
            self.assertEqual(proof['prepared_text_sha256'], scope['source_text_sha256'])
            self.assertEqual(raw, path.read_bytes())

    def test_missing_and_foreign_or_unknown_preparation_cannot_authorize_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()/'source.txt'; path.write_text('one two')
            for state in ({}, {'input_file_path': str(path.with_name('missing.txt'))},
                    {'input_file_path': str(path.with_suffix('.json'))},
                    {'input_file_path': str(path), 'script_generation_input_file': 'another.txt'},
                    {'input_file_path': str(path), 'script_generation_input_file': str(path)},
                    {'input_file_path': str(path), 'script_generation_input_file': str(path),
                     'script_generation_options': {'strip_front_matter': 'false'}}):
                with self.subTest(state=state), self.assertRaises(ValueError):
                    get_scope_source_snapshot(state)
            self.assertEqual(['source.txt'], [p.name for p in path.parent.iterdir()])

    def fixture(self):
        entries = [{'text': text} for text in ['Chapter One', 'First synthetic line.',
                                               'Chapter Two', 'Retained synthetic line.']]
        return '\n'.join(row['text'] for row in entries), entries

    def test_complete_ordered_source_binds_exact_word_range_without_mutation(self):
        source, entries = self.fixture(); before = copy.deepcopy(entries)
        result = get_source_scope(source, entries, 2)
        self.assertEqual((5, 10, 10), (result['retained_start_word'], result['retained_end_word'],
                                     result['source_word_count']))
        self.assertEqual(before, entries)
        self.assertEqual('AA2_text_diff_v1', result['word_policy'])

    def test_missing_extra_and_reordered_source_words_are_refused(self):
        source, entries = self.fixture()
        for changed in (source.replace('First ', ''), source + ' Extra.',
                        source.replace('First synthetic', 'synthetic First')):
            with self.subTest(source=changed), self.assertRaisesRegex(ValueError, 'ordered words differ'):
                get_source_scope(changed, entries, 2)

    def test_repeated_headings_have_an_ordinal_proof_instead_of_a_fuzzy_text_match(self):
        entries = [{'text': text} for text in ['Chapter One', 'Echo.', 'Chapter One', 'Echo.']]
        result = get_source_scope('Chapter One Echo. Chapter One Echo.', entries, 2)
        self.assertEqual((3, 6), (result['retained_start_word'], result['retained_end_word']))

    def test_existing_quote_policy_is_used_and_byte_identity_detects_punctuation_changes(self):
        entries = [{'text': 'Hello'}, {'text': 'there.'}]
        first = get_source_scope('“Hello”—“there.”', entries, 1)
        changed = get_source_scope('Hello there!', entries, 1)
        self.assertEqual((1, 2), (first['retained_start_word'], first['retained_end_word']))
        self.assertNotEqual(first['source_text_sha256'], changed['source_text_sha256'])

    def test_invalid_and_wordless_authority_is_refused(self):
        for source, entries, start in [(None, [{'text': 'one'}, {'text': 'two'}], 1),
                ('---', [{'text': ''}, {'text': ''}], 1),
                ('one two', [{'text': 'one'}, {'text': None}], 1),
                ('one two', [{'text': 'one'}, {'text': 'two'}], True),
                ('one', [{'text': ''}, {'text': 'one'}], 1)]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                get_source_scope(source, entries, start)
