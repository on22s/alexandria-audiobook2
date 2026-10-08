"""Native routes refuse stale/repeated Undo and batch uploads preserve active state."""
import asyncio
import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from project import ProjectManager
from routers import editor, script


class EditorReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_delete_receipt_binds_book_and_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pm = ProjectManager(folder)
            pm.save_chunks([{'id': i, 'speaker': 'A', 'text': 'words ' + str(i), 'status': 'pending'} for i in range(3)])
            state = root / 'state.json'
            state.write_text(json.dumps({'active_book_id': 'book-a', 'book_generation': 'generation-a'}))
            app = FastAPI()
            app.include_router(editor.router)
            with patch.object(editor, 'project_manager', pm), \
                 patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                 patch.object(editor, 'check_global_gpu_lock'), \
                 patch.object(editor, '_deleted_chunk_receipts', {}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    deleted = (await client.delete('/api/chunks/1')).json()
                    request = {'chunk': deleted['deleted'], 'at_index': 1, 'undo_token': deleted['undo_token']}
                    before = (root / 'chunks.json').read_bytes()
                    state.write_text(json.dumps({'active_book_id': 'book-b', 'book_generation': 'generation-b'}))
                    self.assertEqual(409, (await client.post('/api/chunks/restore', json=request)).status_code)
                    self.assertEqual(before, (root / 'chunks.json').read_bytes())
                    state.write_text(json.dumps({'active_book_id': 'book-a', 'book_generation': 'generation-a'}))
                    changed = {**request, 'chunk': {**request['chunk'], 'text': 'Injected words'}}
                    self.assertEqual(409, (await client.post('/api/chunks/restore', json=changed)).status_code)
                    results = await asyncio.gather(*(client.post('/api/chunks/restore', json=request) for _ in range(2)))
                    self.assertEqual([200, 409], sorted(response.status_code for response in results))
                    rows = pm.load_chunks()
                    self.assertEqual(3, len(rows))
                    self.assertEqual(3, len({row['uid'] for row in rows}))
                    self.assertEqual('words 1', rows[1]['text'])
                    self.assertIsNone(pm.restore_chunk(1, rows[1]))

    async def test_batch_staging_and_failed_preflight_do_not_select_an_active_book(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            uploads = root / 'uploads'
            uploads.mkdir()
            state = root / 'state.json'
            state.write_text(json.dumps({'active_book_id': 'selected', 'book_generation': 'stable', 'input_file_path': 'original.txt'}))
            original = state.read_bytes()
            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, 'DATA_DIR', folder), patch.object(script, 'UPLOADS_DIR', str(uploads)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    results = await asyncio.gather(*(client.post('/api/upload?select_active=false',
                        files={'file': (name, b'Uploaded source prose.')}) for name in ('a.txt', 'b.txt')))
                    self.assertEqual([200, 200], [result.status_code for result in results])
                    self.assertEqual(original, state.read_bytes())
                    normal = await client.post('/api/upload', files={'file': ('selected.txt', b'New selected prose.')})
                    self.assertEqual(200, normal.status_code, normal.text)
                    self.assertNotEqual(original, state.read_bytes())
                    self.assertEqual(normal.json()['path'], json.loads(state.read_text())['input_file_path'])


class ConfigDefaultsRace(unittest.TestCase):
    def test_actual_configuration_loader_preserves_edits_during_both_fetches(self):
        script = Path(__file__).with_name('config_defaults_race_fixture.js')
        result = subprocess.run(['node', str(script), str(Path(__file__).parent.parent / 'static/js/app-core.js')], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
