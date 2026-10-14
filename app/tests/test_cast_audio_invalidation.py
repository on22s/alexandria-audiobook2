"""Active cast application must fence old voice audio through the real writer."""
import contextlib
import copy
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient
from project import ProjectManager
from routers import voice_library as library
from tests import test_voice_library_transactions as library_fixtures
from book_state_transaction import get_book_snapshot, get_book_snapshot_token


class CastAudioInvalidationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, same_voice=False):
        with library_fixtures.VoiceLibraryTransactionTests().fixture() as (root, api):
            config = {'Hero': {'type': 'custom', 'voice': 'old' if same_voice else 'active'},
                      'Other': {'type': 'custom', 'voice': 'other'}, 'Alias': {'alias_of': 'Hero'}}
            (root / 'voices.json').write_text(json.dumps(config))
            manager = ProjectManager(str(root))
            manager.voice_config_path = str(root / 'voices.json')
            rows = []
            for index, speaker in enumerate(('Hero', 'Other', 'Alias')):
                audio = root / f'prior_{speaker}.wav'
                sf.write(audio, np.full(800, (index + 1) / 8), 8000)
                rows.append({'uid': f'uid-{speaker}', 'id': index, 'text': f'{speaker} words.',
                             'speaker': speaker, 'instruct': '', 'status': 'done', 'audio_path': str(audio),
                             'voice_revision': 'original-' + speaker, 'audio_revision': 4,
                             'pause_after': 0, 'custom': {'preserve': speaker}})
            manager.save_chunks(rows)
            with TestClient(api) as client:
                yield root, client, manager

    def apply(self, root, client, mapping=None, token=None):
        token = token or get_book_snapshot_token(get_book_snapshot(str(root), include_voices=False))
        return client.post('/api/voice_library/apply', json={
            'book_token': token, 'cast': 'series', 'mapping': mapping or {'Hero': 'hero'}})

    def test_changed_active_cast_invalidates_aliases_and_export_but_preserves_other_audio(self):
        with self.fixture() as (root, client, manager):
            before = copy.deepcopy(manager.load_chunks())
            audio_bytes = {Path(row['audio_path']): Path(row['audio_path']).read_bytes() for row in before}
            response = self.apply(root, client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(['Hero'], response.json()['applied'])
            self.assertEqual('old', json.loads((root / 'voices.json').read_text())['Hero']['voice'])
            after = manager.load_chunks()
            for index in (0, 2):
                self.assertEqual('pending', after[index]['status'])
                self.assertIsNone(after[index]['audio_path'])
                self.assertNotEqual(before[index]['voice_revision'], after[index]['voice_revision'])
                self.assertEqual(before[index]['custom'], after[index]['custom'])
            self.assertEqual(before[1], after[1])
            exported, skipped = manager._load_chunks_with_audio()
            self.assertEqual(1, len(exported))
            self.assertEqual(2, skipped)
            self.assertEqual('Other', exported[0][0]['speaker'])
            self.assertEqual(audio_bytes, {path: path.read_bytes() for path in audio_bytes})

    def test_identical_effective_cast_and_unmapped_members_do_not_invalidate_audio(self):
        for missing in (False, True):
            with self.subTest(missing=missing), self.fixture(same_voice=True) as (root, client, manager):
                before = copy.deepcopy(manager.load_chunks())
                config_bytes = (root / 'voices.json').read_bytes()
                response = self.apply(root, client, {'Hero': 'missing' if missing else 'hero'})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(0 if missing else 1, response.json()['count'])
                self.assertEqual(before, manager.load_chunks())
                if missing:
                    self.assertEqual(config_bytes, (root / 'voices.json').read_bytes())

    def test_wrong_book_token_preserves_config_chunks_and_library_bytes(self):
        with self.fixture() as (root, client, manager):
            paths = [root / 'voices.json', root / 'chunks.json', root / 'library.json']
            before = {path: path.read_bytes() for path in paths}
            response = self.apply(root, client, token='0' * 64)
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_saved_cast_bulk_keeps_active_voice_and_audio_exact(self):
        with self.fixture() as (root, client, manager):
            (root / 'state.json').write_text(json.dumps({'active_book_id': library._get_saved_book_id('one')}))
            paths = [root / 'voices.json', root / 'chunks.json']
            before = {path: path.read_bytes() for path in paths}
            response = client.post('/api/voice_library/apply_bulk', json={
                'cast': 'series', 'mapping': {'Hero': 'hero'}, 'script_names': ['one']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('old', json.loads((root / 'scripts/one.voice_config.json').read_text())['Hero']['voice'])
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_inflight_old_voice_cannot_publish_after_cast_application(self):
        with self.fixture() as (root, client, manager), contextlib.redirect_stdout(io.StringIO()):
            original = copy.deepcopy(manager.load_chunks())
            prior = Path(original[0]['audio_path'])
            prior_bytes = prior.read_bytes()
            entered, release = threading.Event(), threading.Event()
            result, errors = [], []
            def voice(text, instruct, speaker, config, output):
                self.assertEqual('active', config['Hero']['voice'])
                entered.set()
                if not release.wait(5):
                    raise AssertionError('synthetic render not released')
                sf.write(output, np.full(800, .5), 8000)
                return True
            manager.engine = SimpleNamespace(generate_voice=voice)
            def render():
                try:
                    result.append(manager.generate_chunk_audio(0))
                except BaseException as error:
                    errors.append(error)
            with patch('project._export_audio_segment', side_effect=RuntimeError('native WAV fallback')):
                worker = threading.Thread(target=render)
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    response = self.apply(root, client)
                    self.assertEqual(200, response.status_code, response.text)
                    after = copy.deepcopy(manager.load_chunks())
                    self.assertEqual('pending', after[0]['status'])
                finally:
                    release.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, len(result))
            self.assertFalse(result[0][0])
            self.assertIn('generation inputs', result[0][1])
            self.assertEqual(after, manager.load_chunks())
            self.assertEqual(prior_bytes, prior.read_bytes())
            self.assertEqual([], list(root.glob('chunk_*.wav')))
