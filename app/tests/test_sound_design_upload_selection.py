"""Real upload ownership survives slow cue checks, cancellation and late I/O failure."""
import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, UploadFile
import httpx

import book_state_transaction as books
import core
from routers import script
from sound_design_registry import ACTIVE_CUES, get_sound_design_registry_bytes
from tests.test_sound_design_registry import registry
from tests.test_upload_publication_source import epub, TEXT


class SoundDesignUploadSelectionTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); uploads = root / 'uploads'; uploads.mkdir()
            (root / 'state.json').write_text('{"active_book_id":"old","input_file_path":"old.txt"}')
            (root / ACTIVE_CUES).write_bytes(get_sound_design_registry_bytes(registry('old')))
            app = FastAPI(); app.include_router(script.router)
            @app.get('/fixture/ping')
            async def ping():
                return {'ok': True}
            with patch.object(script, 'DATA_DIR', str(root)), patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(core, 'DATA_DIR', str(root)), \
                    patch.object(core, 'process_state', {'script': {'running': False}}), \
                    patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                             base_url='http://fixture') as client:
                    yield root, uploads, client

    async def test_slow_metadata_runs_off_loop_and_releases_only_local_task_mutex(self):
        async with self.fixture() as (root, uploads, client):
            entered, release = threading.Event(), threading.Event()
            capture = __import__('sound_design_renditions').capture_sound_design_companions
            threads = []; loop_thread = threading.get_ident()
            def paused(*args, **kwargs):
                threads.append(threading.get_ident()); entered.set()
                if not release.wait(3):
                    raise AssertionError('metadata worker was not released')
                return capture(*args, **kwargs)
            task = None
            with patch('sound_design_renditions.capture_sound_design_companions', side_effect=paused):
                task = asyncio.create_task(client.post('/api/upload', files={'file': ('new.txt', b'Synthetic source.')}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual({'ok': True}, (await asyncio.wait_for(client.get('/fixture/ping'), 1)).json())
                    # The real kernel barrier remains owned, while the local
                    # task mutex must not strand another event-loop handler.
                    acquired = core._gpu_lock.acquire(blocking=False)
                    self.assertTrue(acquired)
                    if acquired:
                        core._gpu_lock.release()
                    with self.assertRaises(HTTPException) as refused:
                        core.reserve_background_task('script', cpu_only=True)
                    self.assertEqual(400, refused.exception.status_code)
                    self.assertIsInstance(refused.exception.__cause__, core.TaskOwnershipBusy)
                finally:
                    release.set()
                    response = await task
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(threads and all(t != loop_thread for t in threads))
            self.assertFalse((root / ACTIVE_CUES).exists())
            selected = Path(json.loads((root / 'state.json').read_text())['input_file_path'])
            self.assertEqual(b'Synthetic source.', selected.read_bytes())

    async def test_cancel_after_selection_publication_waits_then_preserves_source_and_provenance(self):
        async with self.fixture() as (root, uploads, _client):
            entered, release = threading.Event(), threading.Event()
            publish = books.apply_book_state_locked
            def paused(*args, **kwargs):
                publish(*args, **kwargs); entered.set()
                if not release.wait(3):
                    raise AssertionError('publication worker was not released')
            with patch.object(books, 'apply_book_state_locked', side_effect=paused):
                task = asyncio.create_task(script.upload_file(UploadFile(filename='new.epub', file=io.BytesIO(epub()))))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    selected = Path(json.loads((root / 'state.json').read_text())['input_file_path'])
                    companion = Path(str(selected) + '.publication.json'); before = companion.read_bytes()
                    task.cancel(); await asyncio.sleep(.03)
                    self.assertFalse(task.done(), 'cancellation abandoned the admitted worker')
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual(TEXT, selected.read_bytes()); self.assertEqual(before, companion.read_bytes())
            self.assertFalse((root / ACTIVE_CUES).exists())

    async def test_late_publication_error_preserves_selected_source_for_recovery(self):
        async with self.fixture() as (root, uploads, client):
            publish = books.apply_book_state_locked
            def failed_after_commit(*args, **kwargs):
                publish(*args, **kwargs)
                raise OSError('fixture late publication failure')
            with patch.object(books, 'apply_book_state_locked', side_effect=failed_after_commit):
                response = await client.post('/api/upload', files={'file': ('new.epub', epub())})
            self.assertEqual(500, response.status_code)
            selected = Path(json.loads((root / 'state.json').read_text())['input_file_path'])
            self.assertEqual(TEXT, selected.read_bytes())
            self.assertTrue(Path(str(selected) + '.publication.json').is_file())

    async def test_no_cue_late_atomic_state_error_preserves_selected_source(self):
        async with self.fixture() as (root, uploads, client):
            (root / ACTIVE_CUES).unlink()
            publish = books.save_adapter_publication_bytes
            def failed_after_write(*args, **kwargs):
                publish(*args, **kwargs)
                raise OSError('fixture late atomic state error')
            with patch.object(books, 'save_adapter_publication_bytes', side_effect=failed_after_write):
                response = await client.post('/api/upload', files={'file': ('new.epub', epub())})
            self.assertEqual(500, response.status_code)
            selected = Path(json.loads((root / 'state.json').read_text())['input_file_path'])
            self.assertEqual(TEXT, selected.read_bytes())
            self.assertTrue(Path(str(selected) + '.publication.json').is_file())
            self.assertGreaterEqual(script.get_upload_storage_bytes(), len(TEXT))

    async def test_bad_metadata_refuses_selection_but_retains_publicly_visible_source(self):
        async with self.fixture() as (root, uploads, client):
            before = (root / 'state.json').read_bytes(); (root / ACTIVE_CUES).write_bytes(b'{invalid')
            response = await client.post('/api/upload', files={'file': ('new.txt', b'Synthetic source.')})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (root / 'state.json').read_bytes())
            self.assertEqual(b'{invalid', (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(['new.txt'], [p.name for p in uploads.iterdir()])
            self.assertEqual(b'Synthetic source.', (uploads / 'new.txt').read_bytes())


if __name__ == '__main__':
    unittest.main()
