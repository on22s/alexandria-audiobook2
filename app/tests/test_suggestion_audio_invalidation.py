"""Suggested voices fence completed and in-flight audio through native routes."""
import contextlib
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from routers import voices
from tests import test_cast_audio_invalidation as cast_fixtures


class SuggestionAudioInvalidationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with cast_fixtures.CastAudioInvalidationTests().fixture() as (root, client, manager), contextlib.ExitStack() as patches:
            for key, value in (('SCRIPT_PATH', root / 'annotated_script.json'),
                               ('VOICE_CONFIG_PATH', root / 'voices.json'),
                               ('VOICE_LIBRARY_PATH', root / 'library.json'),
                               ('CHUNKS_PATH', root / 'chunks.json')):
                patches.enter_context(patch.object(voices, key, str(value)))
            patches.enter_context(patch.object(voices, 'project_manager', manager))
            patches.enter_context(patch.object(voices, '_script_line_counts', return_value={'Hero': 1}))
            patches.enter_context(patch.object(voices, '_build_lora_candidates', return_value=[
                {'adapter_id': 'new', 'type': 'lora'}]))
            client.app.include_router(voices.router)
            yield root, client, manager

    def apply(self, root, client, *, bulk=False, cast=None):
        suggestion = {'adapter_id': 'new', 'book_id': 'active-book', 'character_style': 'calm'}
        body = {'suggestions': {'Hero': suggestion}} if bulk else {'character': 'Hero', 'suggestion': suggestion}
        if cast:
            body['cast'] = cast
        return client.post('/api/suggest_voices/apply_bulk' if bulk else '/api/suggest_voices/apply', json=body)

    def assert_affected_only(self, before, after, manager):
        for index in (0, 2):
            self.assertEqual('pending', after[index]['status'])
            self.assertIsNone(after[index]['audio_path'])
            self.assertNotEqual(before[index]['voice_revision'], after[index]['voice_revision'])
            self.assertEqual(before[index]['custom'], after[index]['custom'])
        self.assertEqual(before[1], after[1])
        exported, skipped = manager._load_chunks_with_audio()
        self.assertEqual(['Other'], [item[0]['speaker'] for item in exported])
        self.assertEqual(2, skipped)

    def test_single_and_bulk_with_or_without_cast_invalidate_alias_audio_only(self):
        for bulk in (False, True):
            for cast in (None, 'series'):
                with self.subTest(bulk=bulk, cast=cast), self.fixture() as (root, client, manager):
                    before = copy.deepcopy(manager.load_chunks())
                    audio = {Path(row['audio_path']): Path(row['audio_path']).read_bytes() for row in before}
                    library_before = (root / 'library.json').read_bytes()
                    response = self.apply(root, client, bulk=bulk, cast=cast)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assert_affected_only(before, manager.load_chunks(), manager)
                    self.assertEqual(audio, {path: path.read_bytes() for path in audio})
                    config = json.loads((root / 'voices.json').read_bytes())
                    self.assertEqual('new', config['Hero']['adapter_id'])
                    if cast:
                        library = json.loads((root / 'library.json').read_bytes())
                        self.assertEqual('new', library['casts']['series']['members']['hero']['config']['adapter_id'])
                    else:
                        self.assertEqual(library_before, (root / 'library.json').read_bytes())

    def test_inflight_old_voice_cannot_publish_after_suggestion(self):
        # Reuse the native PCM/held-render scenario with this fixture and route.
        cast_fixtures.CastAudioInvalidationTests.test_inflight_old_voice_cannot_publish_after_cast_application(self)

    def test_reapplying_identical_effective_voice_preserves_audio(self):
        with self.fixture() as (root, client, manager):
            self.assertEqual(200, self.apply(root, client).status_code)
            rows = manager.load_chunks()
            for row in rows:
                row.update(status='done', audio_path=str(root / ('prior_' + row['speaker'] + '.wav')))
            manager.save_chunks(rows)
            before = copy.deepcopy(manager.load_chunks())
            response = self.apply(root, client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, manager.load_chunks())

    def test_manual_persona_recovery_invalidates_changed_style_and_alias_audio(self):
        with self.fixture() as (_root, client, manager):
            before = copy.deepcopy(manager.load_chunks())
            response = client.post('/api/persona/recover', json={'speaker': 'Hero',
                'persona_json': json.dumps({'description': 'A calm adult voice.', 'ref_text': 'Words to say.'})})
            self.assertEqual(200, response.status_code, response.text)
            self.assert_affected_only(before, manager.load_chunks(), manager)

    def test_failed_cast_publication_restores_both_files_and_keeps_old_audio_fenced(self):
        with self.fixture() as (root, client, manager):
            paths = [root / 'voices.json', root / 'library.json']
            before = {path: path.read_bytes() for path in paths}
            chunks = copy.deepcopy(manager.load_chunks())
            replace = os.replace
            failed = []
            def fail_second(source, destination):
                if str(destination) == str(root / 'library.json') and not failed:
                    failed.append(True)
                    fenced = json.loads((root / 'chunks.json').read_bytes())
                    for index in (0, 2):
                        self.assertEqual('pending', fenced[index]['status'])
                        self.assertIsNone(fenced[index]['audio_path'])
                    raise OSError('fixture second publication failed')
                return replace(source, destination)
            with patch('utils.os.replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'fixture second publication failed'):
                    self.apply(root, client, cast='series')
            self.assertEqual([True], failed)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            self.assert_affected_only(chunks, manager.load_chunks(), manager)

    def test_invalid_second_suggestion_cannot_partially_publish_or_invalidate(self):
        with self.fixture() as (root, client, _manager):
            paths = [root / 'voices.json', root / 'library.json', root / 'chunks.json']
            before = {path: path.read_bytes() for path in paths}
            with patch.object(voices, '_script_line_counts', return_value={'Hero': 1, 'Other': 1}):
                response = client.post('/api/suggest_voices/apply_bulk', json={'cast': 'series',
                    'suggestions': {'Hero': {'adapter_id': 'new', 'book_id': 'active-book'},
                                    'Other': {'adapter_id': 'missing', 'book_id': 'active-book'}}})
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_damaged_cast_library_refuses_suggestions_before_any_audio_invalidation(self):
        with self.fixture() as (root, client, _manager):
            library_path = root / 'library.json'
            library = json.loads(library_path.read_bytes())
            library['favorites'] = {}  # Keep the cast readable but corrupt a sibling section.
            library_path.write_text(json.dumps(library))
            paths = [root / 'voices.json', library_path, root / 'chunks.json']
            before = {path: path.read_bytes() for path in paths}
            response = self.apply(root, client, cast='series')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('unchanged', response.json()['detail'])
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
