"""Workspace facts must reject corruption and never initialize/repair book data."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from book_workspace import get_book_workspace
from chunk_status_journal import ChunkStatusJournal
from project import ProjectManager
from routers import scripts_library
from routers.voices import get_voice_rows


class BookWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = ProjectManager(str(self.root))

    def write(self, name, value):
        (self.root/name).write_text(json.dumps(value))

    def prepare(self, voice=None):
        self.write('state.json', {'active_book_id': 'Book A', 'book_generation': 'one'})
        self.write('annotated_script.json', [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}])
        self.write('voice_config.json', {'ALICE': voice or {'type': 'custom', 'description': 'Synthetic voice'}})

    def read(self):
        return get_book_workspace(str(self.root), {'MP3': self.root/'out.mp3', 'M4B': self.root/'out.m4b'},
                                  get_voice_rows, self.manager.get_chunk_audio_path)

    def files(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and not p.name.endswith('.lock')}

    def test_empty_and_missing_chunks_are_distinct_from_completed_audio_and_do_not_write(self):
        before = self.files()
        empty = self.read()
        self.assertEqual(before, self.files())
        self.assertEqual(empty['next']['tab'], 'script')
        self.assertEqual(empty['audio']['status'], 'not_started')
        self.prepare()
        (self.root/'state.json').unlink()
        self.assertEqual(self.read()['name'], 'Current book (unnamed)')
        self.prepare()
        before = self.files()
        state = self.read()
        self.assertEqual(before, self.files())
        self.assertEqual(state['script']['entries'], 1)
        self.assertEqual(state['cast'], {'total': 1, 'pending': 0})
        self.assertEqual(state['next']['tab'], 'editor')
        self.assertFalse((self.root/'chunks.json').exists())

    def test_existing_voice_eligibility_and_pending_state_versions_are_used(self):
        self.prepare({'type': 'custom'})
        self.assertEqual(self.read()['next']['tab'], 'voices')
        # Consume the canonical row builder's pending-state signal; file
        # presence alone must not mark persona state cards prepared.
        state = get_book_workspace(str(self.root), {}, lambda script, voices: [
            {'persona_pending': False, 'persona_states_pending': True}], self.manager.get_chunk_audio_path)
        self.assertEqual(state['cast']['pending'], 1)
        self.assertEqual(state['next']['tab'], 'voices')

    def test_journal_progress_is_read_without_compaction_and_outputs_are_unverified(self):
        self.prepare()
        self.write('chunks.json', [{'uid': 'one', 'id': 0, 'status': 'pending', 'audio_path': None}])
        (self.root/'one.wav').write_bytes(b'synthetic audio presence')
        journal = ChunkStatusJournal(self.root/'chunks.json', {'active_book_id': 'Book A', 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        (self.root/'out.mp3').write_bytes(b'synthetic global output')
        before = self.files()
        state = self.read()
        self.assertEqual(state['audio']['present'], 1)
        self.assertEqual(state['audio']['status'], 'files_present')
        self.assertEqual(state['outputs'][0]['status'], 'present_unverified')
        self.assertEqual(state['next']['tab'], 'audio')
        self.assertIn('verify', state['next']['reason'])
        self.assertEqual(before, self.files())
        self.assertNotIn(self.directory.name, json.dumps(state))
        self.assertNotIn('Synthetic line.', json.dumps(state))

    def test_missing_empty_and_escaping_audio_do_not_count_as_present(self):
        self.prepare()
        self.write('chunks.json', [{'uid': str(i), 'id': i, 'status': 'done', 'audio_path': path}
                                 for i, path in enumerate(['missing.wav', 'empty.wav', '../outside.wav'])])
        (self.root/'empty.wav').touch()
        state = self.read()
        self.assertEqual(state['audio']['present'], 0)
        self.assertEqual(state['audio']['missing'], 3)
        self.assertEqual(state['next']['tab'], 'editor')

    def test_wrong_book_journal_and_corrupt_chunk_data_remain_unavailable_and_untouched(self):
        self.prepare()
        self.write('chunks.json', [{'uid': 'one', 'id': 0, 'status': 'pending'}])
        wrong = ChunkStatusJournal(self.root/'chunks.json', {'active_book_id': 'Other', 'book_generation': 'two'})
        wrong.apply_update('one', {'status': 'done'})
        before = self.files()
        self.assertEqual(self.read()['audio']['status'], 'unavailable')
        self.assertEqual(before, self.files())
        (self.root/'chunks.json').write_text('{')
        before = self.files()
        self.assertEqual(self.read()['audio']['status'], 'unavailable')
        self.assertEqual(before, self.files())

    def test_actual_route_returns_safe_conflict_for_corrupt_script_without_repair(self):
        self.prepare()
        (self.root/'annotated_script.json').write_text('{')
        application = FastAPI(); application.include_router(scripts_library.router)
        before = self.files()
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('AUDIOBOOK_PATH', str(self.root/'out.mp3')),
                               ('M4B_PATH', str(self.root/'out.m4b'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                response = client.get('/api/workspace')
        self.assertEqual(response.status_code, 409)
        self.assertIn('No new chunks or audio', response.json()['detail'])
        self.assertEqual(before, self.files())
