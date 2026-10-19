"""Real chapter grouping and request admission, with no model execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from project import ProjectManager
from tests.test_support import use_private_cpu_fixture_lock


class EditorAudioRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pm = ProjectManager(self.tmp.name)
        rows = [
            {'speaker': 'NARRATOR', 'text': 'An introduction.'},
            {'speaker': 'NARRATOR', 'text': 'Chapter 1'},
            {'speaker': 'Hero', 'text': 'Hello.', 'status': 'done', 'audio_path': 'one.wav'},
            {'speaker': 'Other', 'text': 'Goodbye.'},
            {'speaker': 'NARRATOR', 'text': 'Chapter 1'},
            {'speaker': 'Hero', 'text': 'Later.'},
            {'speaker': 'Hero', 'text': ''}]
        self.pm.save_chunks([{**row, 'id': i, 'uid': f'row-{i}'} for i, row in enumerate(rows)])
        for name, value in [('voice_config.json', {'Hero': {'voice': 'Ryan'}}), ('config.json', {})]:
            (self.root/name).write_text(json.dumps(value))
        for name, value in [('project_manager', self.pm), ('SCRIPT_PATH', str(self.root/'annotated_script.json')),
                            ('CONFIG_PATH', str(self.root/'config.json'))]:
            active = patch.object(editor, name, value); active.start(); self.addCleanup(active.stop)
        active = patch.object(editor.pronunciation, 'DEFAULT_PATH', str(self.root/'pronunciation.json')); active.start(); self.addCleanup(active.stop)
        app = FastAPI(); app.include_router(editor.router)
        self.client = TestClient(app)
        self.token = editor.get_snapshot_digest(editor.get_editor_book_identity(self.tmp.name))

    def preview(self, chapter=None, speakers=None):
        return self.client.post('/api/chunks/repair_preview', json={
            'expected_options_revision': editor.get_snapshot_digest(self.pm.load_chunks()),
            'expected_book_token': self.token, 'chapter': chapter, 'speakers': speakers or ['Hero']})

    def test_shared_chapter_detection_and_literal_speaker_filter(self):
        response = self.client.get('/api/chunks/repair_options', params={'expected_book_token': self.token})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['Introduction', 'Chapter 1', 'Chapter 1'], [c['title'] for c in response.json()['chapters']])
        first = self.preview(1).json()
        self.assertEqual([2], [r['id'] for r in first['rows']])
        self.assertEqual(1, first['rendered_count'])
        self.assertEqual([5], [r['id'] for r in self.preview(2).json()['rows']])
        self.assertEqual([2, 5], [r['id'] for r in self.preview().json()['rows']])
        self.assertEqual([2, 3], [r['id'] for r in self.preview(1, ['Hero', 'Other']).json()['rows']])

    def test_options_revision_rejects_changed_chapter_roster(self):
        revision = editor.get_snapshot_digest(self.pm.load_chunks())
        chunks = self.pm.load_chunks(); chunks[1]['text'] = 'Chapter 99'; self.pm.save_chunks(chunks)
        response = self.client.post('/api/chunks/repair_preview', json={
            'expected_book_token': self.token, 'expected_options_revision': revision, 'speakers': ['Hero'], 'chapter': 1})
        self.assertEqual(409, response.status_code, response.text)

    def test_empty_no_heading_and_invalid_filters(self):
        self.pm.save_chunks([{'id': 0, 'uid': 'x', 'speaker': 'Hero', 'text': 'One sentence.'}, {'id': 1, 'uid': 'y', 'speaker': 'Other', 'text': 'Two sentences.'}])
        self.assertEqual([0], [r['id'] for r in self.preview(0).json()['rows']])
        self.assertEqual(409, self.preview(99).status_code)
        self.assertEqual(409, self.preview(speakers=['hero']).status_code)
        self.assertEqual(422, self.client.post('/api/chunks/repair_preview', json={'expected_book_token': self.token, 'expected_options_revision': '0'*64, 'speakers': []}).status_code)
        self.pm.save_chunks([])
        self.assertEqual([], editor.get_audio_repair_options([])['chapters'])

    @use_private_cpu_fixture_lock()
    def test_both_batch_routes_admit_exact_selection_and_reject_stale_without_worker(self):
        state = copy.deepcopy(editor.process_state)
        with patch.object(editor, 'process_state', state), patch.object(editor, 'check_global_gpu_lock'), \
             patch.object(self.pm, 'generate_chunks_parallel', return_value={'completed': [2], 'failed': []}) as parallel, \
             patch.object(self.pm, 'generate_chunks_batch', return_value={'completed': [2], 'failed': []}) as batch:
            for route, worker in [('/api/generate_batch', parallel), ('/api/generate_batch_fast', batch)]:
                before = self.preview(1).json()
                request = {'indices': [2], 'expected_uids': [before['rows'][0]['uid']],
                           'expected_book_token': self.token, 'expected_repair_revision': before['repair_revision']}
                response = self.client.post(route, json=request)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([2], worker.call_args.args[0])
                self.assertEqual({2}, set(worker.call_args.kwargs['admitted_requests']))
                worker.reset_mock()
                chunks = self.pm.load_chunks(); chunks[2]['text'] += ' Changed.'; self.pm.save_chunks(chunks)
                response = self.client.post(route, json=request)
                self.assertEqual(409, response.status_code, response.text)
                worker.assert_not_called()

    def test_voice_settings_row_and_book_changes_invalidate_admission(self):
        for filename in ('voice_config.json', 'config.json', 'character_aliases.json', 'pronunciation.json'):
            with self.subTest(filename=filename):
                preview = self.preview(1).json()
                (self.root/filename).write_text(json.dumps({'changed': filename}))
                with self.assertRaises(editor.HTTPException) as error:
                    editor.ensure_audio_generation_requests([2], self.token, [preview['rows'][0]['uid']], preview['repair_revision'])
                self.assertEqual(409, error.exception.status_code)
        preview = self.preview(1).json()
        for book, uids in [('a'*64, [preview['rows'][0]['uid']]), (self.token, ['wrong']), (None, None)]:
            with self.assertRaises(editor.HTTPException):
                editor.ensure_audio_generation_requests([2], book, uids, preview['repair_revision'])
        chunks = self.pm.load_chunks(); chunks.reverse(); self.pm.save_chunks(chunks)
        with self.assertRaises(editor.HTTPException):
            editor.ensure_audio_generation_requests([2], self.token, [preview['rows'][0]['uid']], preview['repair_revision'])

    def test_unknown_and_whitespace_distinct_speakers_round_trip_exactly(self):
        rows = [{'id': i, 'uid': f'speaker-{i}', 'text': 'Hello.', 'speaker': label}
                for i, label in enumerate(['', 'Hero', ' Hero '])]
        self.pm.save_chunks(rows)
        response = self.client.get('/api/chunks/repair_options', params={'expected_book_token': self.token})
        self.assertEqual(['', ' Hero ', 'Hero'], response.json()['speakers'])
        for index, speaker in enumerate(['', 'Hero', ' Hero ']):
            with self.subTest(speaker=speaker):
                preview = self.preview(speakers=[speaker])
                self.assertEqual(200, preview.status_code, preview.text)
                self.assertEqual([index], [row['id'] for row in preview.json()['rows']])
