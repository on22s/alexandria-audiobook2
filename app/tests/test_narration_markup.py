"""Opt-in markup preserves original text, identities and retained take bytes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from narration_markup import RECEIPT_KEY, NarrationMarkupError, get_narration_markup_preview
from narration_markup_edit import get_saved_narration_markup_preview, apply_saved_narration_markup
from book_state_transaction import get_book_operation_token
from project import ProjectManager, get_chunk_generation_inputs, get_chapter_text_digest, get_chunk_chapter_start


class NarrationMarkupParserTests(unittest.TestCase):
    def test_unicode_spans_and_original_text_are_exact(self):
        text = '𠀀 café\r\nHello. [[pause:250ms]]\n'
        report = get_narration_markup_preview(text)
        self.assertEqual(text, report['original_text'])
        self.assertEqual('𠀀 café\r\nHello. \n', report['spoken_text'])
        self.assertEqual(250, report['pause_after'])
        for span in report['spans']:
            actual = text[span['start']:span['end']]
            self.assertEqual('[[pause:250ms]]' if span['kind'] == 'pause' else span['text'], actual)

    def test_literal_brackets_and_escaped_openers(self):
        text = '[a note] and [250ms]'
        self.assertEqual(text, get_narration_markup_preview(text)['spoken_text'])
        result = get_narration_markup_preview(r'Read \[[pause:250ms]] literally.')
        self.assertEqual('Read [[pause:250ms]] literally.', result['spoken_text'])
        self.assertIsNone(result['pause_after'])

    def test_invalid_internal_nested_and_voice_directives_are_located(self):
        invalid = ('Hello [[pause:1s]]', 'Hello [[pause:-1ms]]', 'Hello [[pause:760001ms]]',
            'Hello [[pause:1.5ms]]', 'Hello [[pause:10ms]', 'Hello [[pause:[[pause:1ms]]]]',
            'Hello [[pause:5ms]] next', '[[pause:5ms]]', 'Hello [[voice:C:/private]]',
            'Hello [[voice:https://example.invalid]]', 'Hello [[other]]')
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(NarrationMarkupError) as caught:
                get_narration_markup_preview(text)
            self.assertIn('character ', str(caught.exception))

    def test_finite_bounds_and_determinism(self):
        for duration in (0, 760000):
            text = f'Hello [[pause:{duration}ms]]'
            self.assertEqual(duration, get_narration_markup_preview(text)['pause_after'])
            self.assertEqual(get_narration_markup_preview(text), get_narration_markup_preview(text))
        with self.assertRaises(NarrationMarkupError): get_narration_markup_preview('x' * 32769)


class NarrationMarkupEditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pm = ProjectManager(directory.name)
        self.pm.config_path = str(self.root / 'config.json')
        self.source = self.root / 'source.txt'
        self.original = 'Hello. [[pause:250ms]]'
        self.source.write_text(self.original, encoding='utf-8')
        (self.root / 'state.json').write_text(json.dumps({'active_book_id': 'test-book',
            'book_generation': 'generation-a', 'input_file_path': str(self.source)}))
        (self.root / 'annotated_script.json').write_text(json.dumps([{'text': self.original}]))
        (self.root / 'voice_config.json').write_text(json.dumps({'A': {'voice': 'Ryan'}}))
        (self.root / 'config.json').write_text('{}')
        self.rows = [{'id': 0, 'uid': 'line-a', 'text': self.original, 'speaker': 'A',
            'instruct': 'Read clearly', 'pause_after': 500, 'status': 'done',
            'audio_path': 'voicelines/previous.wav', 'audio_revision': 'previous',
            'source_entry_indices': [0], 'chapter': 3, 'drift': {'score': .8}}]
        self.pm.save_chunks(self.rows)
        self.audio = self.root / 'voicelines/previous.wav'
        self.audio.write_bytes(b'original take bytes')
        self.token = get_book_operation_token(self.root)

    def preview(self):
        return get_saved_narration_markup_preview(self.pm, 0, self.token, 'line-a')

    def apply(self, report, choice='keep'):
        return apply_saved_narration_markup(self.pm, 0, self.token, 'line-a', report['binding'], choice)

    def test_preview_and_cancel_are_read_only_and_default_dispatch_is_literal(self):
        before = (self.root / 'chunks.json').read_bytes()
        with patch.object(self.pm, 'get_engine', side_effect=AssertionError('No inference')):
            report = self.preview()
        self.assertTrue(report['conflict'])
        self.assertEqual(500, report['existing_pause'])
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())
        self.assertIn('[[pause:', get_chunk_generation_inputs(self.pm.get_saved_chunks()[0])['text'])

    def test_conflict_requires_explicit_resolution(self):
        report = self.preview()
        with self.assertRaisesRegex(ValueError, 'explicitly keep'): self.apply(report, 'apply')
        self.assertEqual(self.rows, self.pm.get_saved_chunks())
        self.apply(report, 'replace')
        self.assertEqual(250, self.pm.get_saved_chunks()[0]['pause_after'])

    def test_source_facts_move_to_receipt_instead_of_describing_changed_words(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(source_span={'start': 0, 'end': len(self.original)}, spoken=True)
        self.pm.save_chunks(rows)
        self.apply(self.preview())
        saved = self.pm.get_saved_chunks()[0]
        self.assertNotIn('source_span', saved)
        self.assertNotIn('spoken', saved)
        self.assertEqual({'source_span': rows[0]['source_span'], 'spoken': True},
                         saved[RECEIPT_KEY]['source_facts'])

    def test_existing_occurrence_decisions_require_explicit_author_resolution(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['pronunciation_rules'] = [{'state': 'approved'}]
        self.pm.save_chunks(rows)
        before = (self.root / 'chunks.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'occurrence pronunciation decisions'):
            self.preview()
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())

    def test_application_preserves_export_chapter_roles(self):
        for role in (False, True, None):
            with self.subTest(role=role):
                rows = copy.deepcopy(self.rows)
                rows[0].update(text='Morning[[pause:250ms]]', speaker='NARRATOR')
                if role is not None:
                    rows[0]['chapter_origin'] = {'is_heading': role,
                        'text_sha256': get_chapter_text_digest(rows[0]['text'])}
                else:
                    rows[0]['spoken'] = True
                self.pm.save_chunks(rows)
                header = {'text': 'Chapter 1', 'speaker': 'NARRATOR'}
                before = self.pm._chapter_groups([header, rows[0]], per_chunk_chapters=False)
                self.apply(self.preview())
                saved = self.pm.get_saved_chunks()[0]
                self.assertIs(get_chunk_chapter_start(saved), role is True)
                after = self.pm._chapter_groups([header, saved], per_chunk_chapters=False)
                self.assertEqual([(start, end) for _title, start, end in before],
                                 [(start, end) for _title, start, end in after])

    def test_repeated_authored_applications_keep_prior_receipts_and_have_a_limit(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['source_span'] = {'start': 0, 'end': len(self.original)}
        self.pm.save_chunks(rows)
        self.apply(self.preview())
        first = copy.deepcopy(self.pm.get_saved_chunks()[0][RECEIPT_KEY])
        for index in range(1, 8):
            self.pm.update_chunk(0, {'text': f'New words {index} [[pause:250ms]]'})
            self.apply(self.preview())
            receipt = self.pm.get_saved_chunks()[0][RECEIPT_KEY]
            self.assertEqual(first, receipt['prior_applications'][0])
            self.assertEqual(index, len(receipt['prior_applications']))
        self.pm.update_chunk(0, {'text': 'Another [[pause:250ms]]'})
        before = (self.root / 'chunks.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'history is full'):
            self.preview()
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())

    def test_reauthoring_an_applied_literal_requires_explicit_reescaping(self):
        self.pm.update_chunk(0, {'text': r'She typed \[[ then stopped'})
        self.apply(self.preview(), 'apply')
        self.pm.update_chunk(0, {'text': 'She typed [[ then stopped.'})
        before = (self.root / 'chunks.json').read_bytes()
        with self.assertRaisesRegex(NarrationMarkupError, 'escape literal'):
            self.preview()
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())

    def test_atomic_receipt_survives_reload_without_reapplication_or_source_mutation(self):
        report = self.preview()
        self.assertEqual('applied', self.apply(report)['status'])
        saved = self.pm.get_saved_chunks()[0]
        self.assertEqual(('Hello. ', 500, 'line-a', [0], 3, 'Read clearly'),
            tuple(saved[key] for key in ('text', 'pause_after', 'uid', 'source_entry_indices', 'chapter', 'instruct')))
        self.assertEqual('pending', saved['status'])
        self.assertIsNone(saved['audio_path'])
        self.assertEqual(self.original, saved[RECEIPT_KEY]['original_text'])
        self.assertEqual(b'original take bytes', self.audio.read_bytes())
        self.assertEqual(self.original, self.source.read_text())
        self.assertEqual(self.original, json.loads((self.root / 'annotated_script.json').read_text())[0]['text'])
        reloaded = ProjectManager(str(self.root)); reloaded.config_path = self.pm.config_path
        before = (self.root / 'chunks.json').read_bytes()
        self.assertEqual('already_applied', apply_saved_narration_markup(reloaded, 0,
            self.token, 'line-a', report['binding'], 'keep')['status'])
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())
        self.assertNotIn('[[pause:', get_chunk_generation_inputs(saved)['text'])

    def test_changed_source_script_voices_settings_and_editor_refuse(self):
        for name in ('source.txt', 'annotated_script.json', 'voice_config.json', 'config.json', 'chunks.json'):
            with self.subTest(name=name):
                path = self.root / name; original = path.read_bytes(); report = self.preview()
                if name == 'source.txt': path.write_bytes(b'Changed source')
                else:
                    value = json.loads(original)
                    if isinstance(value, list): value[0]['text'] += 'changed'
                    else: value['changed'] = True
                    path.write_text(json.dumps(value))
                before = (self.root / 'chunks.json').read_bytes()
                with self.assertRaises(ValueError): self.apply(report)
                self.assertEqual(before, (self.root / 'chunks.json').read_bytes())
                path.write_bytes(original)

    def test_wrong_book_uid_and_duplicate_after_edit_refuse(self):
        report = self.preview()
        with self.assertRaises(ValueError):
            get_saved_narration_markup_preview(self.pm, 0, '0' * 64, 'line-a')
        with self.assertRaises(ValueError):
            get_saved_narration_markup_preview(self.pm, 0, self.token, 'wrong')
        self.apply(report); self.pm.update_chunk(0, {'pause_after': 999})
        with self.assertRaises(ValueError): self.apply(report)
        self.assertEqual(999, self.pm.get_saved_chunks()[0]['pause_after'])

    def test_write_failure_preserves_authoring_state_and_audio(self):
        report = self.preview(); before = (self.root / 'chunks.json').read_bytes()
        with patch.object(self.pm, '_save_chunks_locked', side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.apply(report)
        self.assertEqual(before, (self.root / 'chunks.json').read_bytes())
        self.assertEqual(b'original take bytes', self.audio.read_bytes())

    def test_escaped_literal_application_does_not_set_pause(self):
        rows = copy.deepcopy(self.rows); rows[0]['text'] = r'Literal \[[pause:250ms]]'
        self.pm.save_chunks(rows); report = self.preview()
        self.assertIsNone(report['pause_after'])
        self.apply(report, 'apply')
        self.assertEqual('Literal [[pause:250ms]]', self.pm.get_saved_chunks()[0]['text'])
        self.assertEqual(500, self.pm.get_saved_chunks()[0]['pause_after'])
        for regenerate in (False, True):
            with self.subTest(regenerate=regenerate):
                if regenerate:
                    self.pm.update_chunk(0, {'take': 1})
                before = (self.root / 'chunks.json').read_bytes()
                repeated = self.preview()
                self.assertFalse(repeated['changed'])
                self.assertIsNone(repeated['pause_after'])
                self.assertEqual(r'Literal \[[pause:250ms]]',
                                 repeated['previous_application']['original_text'])
                with self.assertRaisesRegex(ValueError, 'No directive'):
                    self.apply(repeated, 'apply')
                self.assertEqual(before, (self.root / 'chunks.json').read_bytes())
