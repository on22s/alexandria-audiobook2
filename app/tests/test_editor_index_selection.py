import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from project import CHAPTER_EXPORT_DIR, ProjectManager
from tests import test_chapter_export as chapter_fixture


class EditorIndexSelectionTests(unittest.TestCase):
    def test_generation_routes_reject_invalid_requests_before_scheduling(self):
        with tempfile.TemporaryDirectory() as root:
            pm = ProjectManager(root)
            pm.save_chunks([{'speaker': 'Hero', 'text': 'Hello'} for _ in range(3)])
            pm.load_chunks()  # Complete the existing legacy UID migration before measuring writes.
            before = Path(pm.chunks_path).read_bytes()
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'project_manager', pm), \
                 patch.object(editor, 'check_global_gpu_lock'), \
                 patch.object(editor, 'load_app_config', return_value={}), \
                 patch.object(editor, 'schedule_claimed_background_task') as schedule, TestClient(app) as client:
                for route in ('/api/generate_batch', '/api/generate_batch_fast'):
                    for indices in ([], [-1], [3], [999999], [0, 3], [True], ['1'], [1.5]):
                        with self.subTest(route=route, indices=indices):
                            response = client.post(route, json={'indices': indices})
                            self.assertEqual(422, response.status_code, response.text)
                            schedule.assert_not_called()
                            self.assertEqual(before, Path(pm.chunks_path).read_bytes())

    def test_generation_workers_receive_unique_source_order_and_correct_total(self):
        with tempfile.TemporaryDirectory() as root:
            pm = ProjectManager(root)
            pm.save_chunks([{'speaker': 'Hero', 'text': 'Hello'} for _ in range(3)])
            app = FastAPI(); app.include_router(editor.router)
            def schedule(tasks, name, callback):
                self.assertEqual('audio', name)
                tasks.add_task(callback)
            state = copy.deepcopy(editor.process_state)
            with patch.object(editor, 'project_manager', pm), \
                 patch.object(editor, 'process_state', state), \
                 patch.object(editor, 'check_global_gpu_lock'), \
                 patch.object(editor, 'load_app_config', return_value={}), \
                 patch.object(editor, 'schedule_claimed_background_task', side_effect=schedule), \
                 patch.object(pm, 'generate_chunks_parallel', return_value={'completed': [0, 2], 'failed': []}) as parallel, \
                 patch.object(pm, 'generate_chunks_batch', return_value={'completed': [0, 2], 'failed': []}) as batch, TestClient(app) as client:
                for route, worker in (('/api/generate_batch', parallel), ('/api/generate_batch_fast', batch)):
                    response = client.post(route, json={'indices': [2, 0, 2]})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(2, response.json()['total_chunks'])
                    self.assertEqual([0, 2], worker.call_args.args[0])
                    self.assertFalse(state['audio']['running'])

    def test_chapter_routes_validate_against_real_audio_groups_before_schedule(self):
        with tempfile.TemporaryDirectory() as root:
            pm, chunks = chapter_fixture._project(root)
            pm.save_chunks(chunks)
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'project_manager', pm), \
                 patch.object(editor, 'schedule_claimed_background_task') as schedule, TestClient(app) as client:
                for per_chunk, count in ((False, 2), (True, 5)):
                    for indices in ([], [-1], [count], [0, count], [True], ['1']):
                        with self.subTest(per_chunk=per_chunk, indices=indices):
                            response = client.post('/api/export_chapters', json={
                                'format': 'wav', 'per_chunk_chapters': per_chunk, 'chapters': indices})
                            self.assertEqual(422, response.status_code, response.text)
                            schedule.assert_not_called()
                            self.assertFalse(Path(root, CHAPTER_EXPORT_DIR).exists())
                # A missing clip changes the per-chunk chapter roster, rather than
                # accepting the old source-row count as the chapter count.
                Path(root, chunks[-1]['audio_path']).unlink()
                response = client.post('/api/export_chapters', json={
                    'format': 'wav', 'per_chunk_chapters': True, 'chapters': [4]})
                self.assertEqual(422, response.status_code, response.text)
                schedule.assert_not_called()

    def test_chapter_route_forwards_normalized_selection_to_real_export(self):
        with tempfile.TemporaryDirectory() as root:
            pm, chunks = chapter_fixture._project(root)
            pm.save_chunks(chunks)
            app = FastAPI(); app.include_router(editor.router)
            state = copy.deepcopy(editor.process_state)
            def schedule(tasks, name, callback):
                self.assertEqual('chapter_export', name)
                tasks.add_task(callback)
            with patch.object(editor, 'project_manager', pm), \
                 patch.object(editor, 'process_state', state), \
                 patch.object(editor, 'schedule_claimed_background_task', side_effect=schedule), \
                 patch.object(pm, 'export_chapters', wraps=pm.export_chapters) as export, TestClient(app) as client:
                response = client.post('/api/export_chapters', json={'format': 'wav', 'chapters': [1, 0, 1]})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([0, 1], export.call_args.kwargs['chapters'])
                manifest = json.loads(Path(root, CHAPTER_EXPORT_DIR, 'manifest.json').read_text())
                self.assertEqual([0, 1], [row['index'] for row in manifest['chapters']])
                self.assertFalse(state['chapter_export']['running'])

    def test_direct_export_rejects_stale_selection_without_changing_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            pm, chunks = chapter_fixture._project(root)
            pm.save_chunks(chunks)
            ok, message = pm.export_chapters(fmt='wav')
            self.assertTrue(ok, message)
            out = Path(root, CHAPTER_EXPORT_DIR)
            before = {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()}
            for changed_only in (False, True):
                for indices in ([], [-1], [2], [0, 999999]):
                    with self.subTest(changed_only=changed_only, indices=indices):
                        ok, message = pm.export_chapters(fmt='wav', chapters=indices, changed_only=changed_only)
                        self.assertFalse(ok, message)
                        self.assertIn('index' if not indices else 'indices', message)
                        self.assertEqual(before, {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()})
            ok, message = pm.export_chapters(fmt='wav', chapters=[1, 0, 1])
            self.assertTrue(ok, message)
            self.assertIn('2 chapter file(s) written', message)
