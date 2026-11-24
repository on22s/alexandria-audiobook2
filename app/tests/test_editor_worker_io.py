import asyncio
import builtins
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
import core
from book_state_transaction import ensure_book_state
from routers import editor
from project import ProjectManager
from tests.test_support import use_private_cpu_fixture_lock


class EditorWorkerIoTests(unittest.TestCase):
    @use_private_cpu_fixture_lock()
    def test_native_io_and_chunk_edits_leave_same_event_loop_ping_responsive(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pm = ProjectManager(str(root)); pm.save_chunks([
                {'id': 0, 'speaker': 'Hero', 'text': 'Hello', 'status': 'pending'},
                {'id': 1, 'speaker': 'Hero', 'text': 'Other', 'status': 'pending'}])
            (root / 'config.json').write_text('{}')
            script = root / 'script.json'; script.write_text('[{"speaker":"Hero","text":"Hello"}]')
            reports = root / 'reports'; reports.mkdir(); report = reports / 'review.md'; report.write_text('Real report')
            scripts = root / 'scripts'; scripts.mkdir()
            checkpoint = scripts / 'book.json.review_checkpoint.json'
            checkpoint.write_text(json.dumps({'completed_batches': 1, 'total_batches': 2, 'all_corrected': [{}]}))
            chapters = root / 'chapter_exports'; chapters.mkdir(); audio = chapters / 'one.wav'; audio.write_bytes(b'clip')
            manifest = chapters / 'manifest.json'; manifest.write_text(json.dumps({'chapters': [{'index': 0, 'file': audio.name}]}))
            book = root / 'book.mp3'; book.write_bytes(b'book audio')
            state = copy.deepcopy(editor.process_state)
            for entry in state.values(): entry['running'] = False
            app = FastAPI(); app.include_router(editor.router)
            @app.get('/ping')
            async def ping(): return {'ok': True}
            cases = [('chunks', 'GET', '/api/chunks', None, pm, 'load_chunks'),
                     ('compact listing', 'GET', '/api/chunks/status', None, pm, 'load_chunks'),
                     ('edit', 'POST', '/api/chunks/0', {'text': 'Updated'}, pm, 'update_chunk'),
                     ('insert', 'POST', '/api/chunks/0/insert', {}, pm, 'insert_chunk'),
                     ('delete', 'DELETE', '/api/chunks/1', None, pm, 'delete_chunk'),
                     ('restore', 'POST', '/api/chunks/restore', {'at_index': 1, 'chunk': {'speaker': 'Hero', 'text': 'Restored'}}, pm, 'restore_chunk'),
                     ('single generation read', 'POST', '/api/chunks/0/generate', {}, pm, 'load_chunks'),
                     ('parallel config', 'POST', '/api/generate_batch', {'indices': [0]}, editor, 'load_app_config'),
                     ('batch config', 'POST', '/api/generate_batch_fast', {'indices': [0]}, editor, 'load_app_config'),
                     ('reports', 'GET', '/api/reports', None, editor.os, 'listdir'),
                     ('report', 'GET', '/api/reports/review.md', None, builtins, 'open'),
                     ('checkpoints', 'GET', '/api/review/checkpoints', None, editor, '_summarize_review_checkpoint'),
                     ('chapter list', 'GET', '/api/chapter_exports', None, editor, 'safe_load_json'),
                     ('main zip', 'GET', '/api/export_zip', None, editor, 'build_zip_download'),
                     ('chapter zip', 'GET', '/api/chapter_exports/zip', None, editor, 'build_zip_download'),
                     ('idle cancel', 'POST', '/api/cancel_audio', {}, pm, 'load_chunks')]
            with patch.object(editor, 'project_manager', pm), patch.object(editor, 'SCRIPT_PATH', str(script)), \
                 patch.object(editor, 'DATA_DIR', str(root)), patch.object(editor, 'CONFIG_PATH', str(root / 'config.json')), \
                 patch.object(editor, 'schedule_claimed_background_task'), patch.object(editor, 'REPORTS_DIR', str(reports)), \
                 patch.object(editor, 'SCRIPTS_DIR', str(scripts)), patch.object(editor, 'AUDIOBOOK_PATH', str(book)), \
                 patch.object(editor, 'M4B_PATH', str(root / 'absent.m4b')), patch.object(editor, 'process_state', state):
                for label, method, url, data, owner, name in cases:
                    with self.subTest(operation=label):
                        if label == "restore":
                            data = {"at_index": 1, "chunk": self.deleted_response["deleted"],
                                    "undo_token": self.deleted_response["undo_token"]}
                        arrived, release = threading.Event(), threading.Event()
                        original = getattr(owner, name)
                        worker_ids = []
                        def paused(*args, **kwargs):
                            matches = True
                            if name == 'open': matches = str(args[0]) == str(report)
                            if name == 'listdir': matches = str(args[0]) == str(reports)
                            if name == 'safe_load_json': matches = str(args[0]) == str(manifest)
                            if matches:
                                worker_ids.append(threading.get_ident()); arrived.set()
                                if not release.wait(3): raise TimeoutError('fixture IO not released')
                            return original(*args, **kwargs)
                        async def run():
                            loop_id = threading.get_ident()
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                                params = None
                                if label == 'edit':
                                    params = {'expected_uid':pm.load_chunks()[0]['uid'],
                                              'expected_book_token':editor.get_snapshot_digest(editor.get_editor_book_identity(str(root)))}
                                pending = asyncio.create_task(client.request(method, url, json=data, params=params))
                                watchdog = threading.Timer(0.5, release.set); watchdog.start()
                                try:
                                    self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                                    response = await client.get('/ping')
                                    self.assertEqual(200, response.status_code)
                                    self.assertFalse(release.is_set(), 'IO completed before ping could run')
                                    self.assertFalse(pending.done(), 'target request blocked the event loop until IO completed')
                                    self.assertTrue(all(identity != loop_id for identity in worker_ids))
                                finally:
                                    release.set(); watchdog.cancel()
                                    response = await pending
                                self.assertEqual(200, response.status_code, response.text[:200])
                                if label == "delete":
                                    self.deleted_response = response.json()
                        with patch.object(owner, name, side_effect=paused): asyncio.run(run())
            self.assertEqual('Updated', pm.load_chunks()[0]['text'])
            self.assertFalse(list(root.glob('.alexandria-export-*.zip')))

    @use_private_cpu_fixture_lock()
    def test_idle_reset_fences_real_audio_claim_until_chunk_write_finishes(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            pm = ProjectManager(root)
            pm.save_chunks([{'speaker': 'Hero', 'text': 'Hello', 'status': 'generating'}])
            script = Path(root) / 'annotated_script.json'
            script.write_text('[{"speaker":"Hero","text":"Hello"}]')
            state = copy.deepcopy(core.process_state)
            for entry in state.values():
                entry['running'] = False
            arrived, release = threading.Event(), threading.Event()
            update_chunk = pm._update_chunk_fields_by_uid

            def paused_update(*args, **kwargs):
                arrived.set()
                if not release.wait(8):
                    raise AssertionError('idle reset fixture not released')
                return update_chunk(*args, **kwargs)

            def reserve_audio():
                with editor.ensure_audio_generation_claim() as claim:
                    return claim

            def assert_book_fenced():
                with self.assertRaises(TimeoutError):
                    with ensure_book_state(root, timeout=0):
                        self.fail('another book operation entered during the idle reset')

            async def run():
                pending = asyncio.create_task(editor.cancel_audio())
                try:
                    self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                    # The coherent owner validator on a different writer must
                    # still be able to take this mutex while the reset does I/O.
                    self.assertTrue(core._gpu_lock.acquire(blocking=False))
                    core._gpu_lock.release()
                    await asyncio.to_thread(assert_book_fenced)
                    with self.assertRaises(HTTPException) as refusal:
                        await asyncio.wait_for(asyncio.to_thread(reserve_audio), timeout=2)
                    self.assertIn('Task admission or startup recovery is in progress',
                                  refusal.exception.detail)
                    self.assertNotIn('audio', core._task_claims)
                    self.assertFalse(state['audio']['running'])
                finally:
                    release.set()
                    result = await pending
                self.assertEqual({'status': 'not_running', 'reset_chunks': 1}, result)
                self.assertEqual('pending', pm.load_chunks()[0]['status'])
                claim = await asyncio.to_thread(reserve_audio)
                self.assertEqual(claim, core._task_claims['audio']['id'])
                self.assertTrue(state['audio']['running'])

            for module, name, value in (
                (core, 'process_state', state), (editor, 'process_state', state),
                (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                (core, 'DATA_DIR', root), (editor, 'DATA_DIR', root),
                (editor, 'SCRIPT_PATH', str(script)), (editor, 'project_manager', pm),
            ):
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(patch.object(pm, '_update_chunk_fields_by_uid', side_effect=paused_update))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
            try:
                asyncio.run(run())
            finally:
                for name, owner in list(core._task_claims.items()):
                    core.release_gpu_task_claim(name, owner['id'])

    def test_running_cancel_sets_flag_without_reading_or_resetting_chunks(self):
        state = copy.deepcopy(editor.process_state); state['audio']['running'] = True
        state['audio']['cancel'] = False; state['audio']['logs'] = []
        with patch.object(editor, 'process_state', state), \
             patch.object(editor.project_manager, 'load_chunks', side_effect=AssertionError('running task chunks must not be reset')):
            result = asyncio.run(editor.cancel_audio())
        self.assertEqual({'status': 'cancelling'}, result)
        self.assertTrue(state['audio']['cancel'])
        self.assertTrue(state['audio']['running'])
