"""Synthetic API regressions for portable casts and book style/state timelines."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient
from routers import voices
from book_state_transaction import get_book_snapshot, get_book_snapshot_token
from tests import test_voice_followups_733 as audio_fixtures
from tests import test_voice_library_transactions as cast_fixtures
from tests import test_state_personas as state_fixtures
from tts import active_character_style, voice_config_for_chunk


class PortableStyleTimelineTests(unittest.TestCase):
    def test_save_and_apply_new_or_legacy_cast_preserve_destination_timeline(self):
        for legacy in (False, True):
            for own_points in ([], [{'from_index': 4, 'character_style': 'B elderly'}]):
                with self.subTest(legacy=legacy, own_points=own_points), \
                     cast_fixtures.VoiceLibraryTransactionTests().fixture() as (root, api), TestClient(api) as client:
                    config_path = root / 'voices.json'
                    source_points = [{'from_index': 2, 'character_style': 'A elderly'}]
                    source = {'type': 'custom', 'voice': 'Ryan', 'character_style': 'A base',
                              'versions': {'manual': {'type': 'custom', 'voice': 'Serena'}},
                              'version_timeline': [{'from_index': 2, 'version_id': 'manual'}],
                              'style_timeline': source_points}
                    config_path.write_text(json.dumps({'Hero': source}))
                    response = client.post('/api/voice_library/save', json={'cast': 'series', 'characters': ['Hero']})
                    self.assertEqual(200, response.status_code, response.text)
                    library_path = root / 'library.json'
                    library = json.loads(library_path.read_text())
                    portable = library['casts']['series']['members']['hero']['config']
                    self.assertNotIn('style_timeline', portable)
                    self.assertNotIn('version_timeline', portable)
                    self.assertIn('manual', portable['versions'])
                    if legacy:
                        portable.update(style_timeline=source_points, version_timeline=source['version_timeline'])
                        library_path.write_text(json.dumps(library))
                    (root / 'state.json').write_text('{"active_book_id":"B","book_generation":"B"}')
                    destination = {'type': 'custom', 'voice': 'Dylan', 'character_style': 'B base',
                                   'style_timeline': own_points,
                                   'version_timeline': [{'from_index': 7, 'version_id': 'manual'}]}
                    config_path.write_text(json.dumps({'Hero': destination}))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([{'speaker': 'Hero', 'text': 'One line.'}]))
                    token = get_book_snapshot_token(get_book_snapshot(str(root), include_voices=False))
                    response = client.post('/api/voice_library/apply', json={
                        'cast': 'series', 'mapping': {'Hero': 'hero'}, 'book_token': token})
                    self.assertEqual(200, response.status_code, response.text)
                    saved = json.loads(config_path.read_text())['Hero']
                    self.assertEqual(own_points, saved['style_timeline'])
                    self.assertEqual(destination['version_timeline'], saved['version_timeline'])
                    self.assertEqual('A base', active_character_style(saved, 2))
                    self.assertEqual('B elderly' if own_points else 'A base', active_character_style(saved, 4))


class StyleAudioInvalidationTests(unittest.TestCase):
    fixture = audio_fixtures.VoiceTimelineAudioTests.fixture

    def save_point(self, client, style='angry'):
        response = client.post('/api/voices/A/style_timeline', json={'from_index': 2, 'character_style': style})
        self.assertEqual(200, response.status_code, response.text)

    def test_add_noop_replace_and_remove_invalidate_only_affected_audio_and_exports(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, config, rows = self.fixture(directory, stack)
            config['A']['style_timeline'] = [{'from_index': 3, 'character_style': ''}]
            path.write_text(json.dumps(config))
            original_audio = {row['audio_path']: (root / row['audio_path']).read_bytes() for row in rows}
            self.save_point(client)
            saved = manager.load_chunks()
            self.assertEqual('pending', saved[2]['status'])
            self.assertIsNone(saved[2]['audio_path'])
            self.assertTrue(saved[2]['voice_revision'])
            for index in (0, 1, 3):
                self.assertEqual(rows[index], saved[index])
            self.assertEqual([0, 1, 3], [row['id'] for row, _segment in manager._load_chunks_with_audio()[0]])
            before = Path(manager.chunks_path).read_bytes()
            self.save_point(client)
            self.assertEqual(before, Path(manager.chunks_path).read_bytes())
            for action in ('replace', 'remove'):
                current = manager.load_chunks()
                revision = current[2]['voice_revision']
                current[2].update(status='done', audio_path=rows[2]['audio_path'])
                manager.save_chunks(current)
                if action == 'replace':
                    self.save_point(client, 'quiet')
                else:
                    response = client.delete('/api/voices/A/style_timeline/2')
                    self.assertEqual(200, response.status_code, response.text)
                saved = manager.load_chunks()
                self.assertEqual('pending', saved[2]['status'])
                self.assertIsNone(saved[2]['audio_path'])
                self.assertNotEqual(revision, saved[2]['voice_revision'])
                for index in (0, 1, 3):
                    self.assertEqual(rows[index], saved[index])
            for name, data in original_audio.items():
                self.assertEqual(data, (root / name).read_bytes())

    def test_add_and_remove_during_render_reject_stale_audio_publication(self):
        for action in ('add', 'remove'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root, manager, client, path, config, rows = self.fixture(directory, stack)
                config['A']['style_timeline'] = [{'from_index': 3, 'character_style': ''}]
                if action == 'remove':
                    config['A']['style_timeline'].insert(0, {'from_index': 2, 'character_style': 'angry'})
                path.write_text(json.dumps(config))
                original = (root / rows[2]['audio_path']).read_bytes()
                def render(text, instruct, speaker, config, output):
                    if action == 'add':
                        self.save_point(client)
                    else:
                        response = client.delete('/api/voices/A/style_timeline/2')
                        self.assertEqual(200, response.status_code, response.text)
                    sf.write(output, np.full(240, .8), 24000)
                    return True
                manager.engine = SimpleNamespace(generate_voice=render)
                success, message = manager.generate_chunk_audio(2)
                self.assertFalse(success)
                self.assertIn('generation inputs', message)
                saved = manager.load_chunks()
                self.assertEqual('pending', saved[2]['status'])
                self.assertIsNone(saved[2]['audio_path'])
                self.assertEqual(original, (root / rows[2]['audio_path']).read_bytes())
                for index in (0, 1, 3):
                    self.assertEqual('done', saved[index]['status'])


class RepeatedStatePositionTests(unittest.TestCase):
    def test_manual_voice_suggestions_use_positions_for_identical_dialogue_and_merged_chunks(self):
        for merge in ('none', 'within_state', 'across_boundary', 'edited'):
            with self.subTest(merge=merge), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                client, path, _ = state_fixtures.StateVoiceApiTests().fixture(directory, stack)
                root = Path(directory)
                script = [{'speaker': 'ARTHUR', 'text': 'Yes.', 'speaker_gender': 'male',
                           'speaker_age_group': age} for age in ('teen', 'adult') for _ in range(12)]
                (root / 'annotated_script.json').write_text(json.dumps(script))
                chunks = copy.deepcopy(script)
                boundary = 12
                if merge in ('within_state', 'across_boundary'):
                    index = 0 if merge == 'within_state' else 11
                    chunks[index]['text'] += ' ' + chunks[index + 1]['text']
                    del chunks[index + 1]
                    boundary -= 1
                elif merge == 'edited':
                    chunks[0]['text'] = 'Changed dialogue.'
                (root / 'chunks.json').write_text(json.dumps(chunks))
                path.write_text(json.dumps({'ARTHUR': {'type': 'custom', 'voice': 'Ryan',
                    'versions': {'adult': {'type': 'custom', 'voice': 'Serena', 'age_group': 'adult', 'gender': 'male'}}}}))
                stack.enter_context(patch.object(voices, 'get_state_voice_candidates', return_value=[]))
                response = client.get('/api/voices/ARTHUR/state_timeline')
                self.assertEqual(200, response.status_code, response.text)
                states = response.json()['states']
                self.assertEqual([0, 12], [state['from_entry'] for state in states])
                expected = [None, None] if merge == 'edited' else [0, None if merge == 'across_boundary' else boundary]
                self.assertEqual(expected, [state['from_index'] for state in states])
                self.assertEqual('adult', states[1]['sources']['versions'][0]['version_id'])
                if states[1]['from_index'] is None:
                    continue
                response = client.post('/api/voices/ARTHUR/version_timeline', json={
                    'points': [{'from_index': boundary, 'version_id': 'adult'}]})
                self.assertEqual(200, response.status_code, response.text)
                saved = json.loads(path.read_text())
                self.assertEqual('Ryan', voice_config_for_chunk(saved, 'ARTHUR', boundary - 1)['ARTHUR']['voice'])
                self.assertEqual('Serena', voice_config_for_chunk(saved, 'ARTHUR', boundary)['ARTHUR']['voice'])
