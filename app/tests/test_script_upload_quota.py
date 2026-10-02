"""Native storage limits include simultaneous uploads and EPUB expansion."""
import asyncio
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI, UploadFile
import httpx
from routers import script


class ScriptUploadQuotaTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        app = FastAPI()
        app.include_router(script.router)
        @app.get('/fixture/ping')
        async def ping():
            return {'ok': True}
        return app

    async def test_unsupported_extensions_store_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    for filename in ('payload.bin', 'payload.exe', 'unsupported.pdf'):
                        with self.subTest(filename=filename):
                            result = await client.post('/api/upload', files={'file': (filename, b'not a source document')})
                            self.assertEqual(400, result.status_code, result.text)
                            self.assertEqual([], list(uploads.iterdir()))

    async def test_parallel_admissions_cannot_both_consume_the_same_free_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            entered, release = threading.Event(), threading.Event()
            original = script._save_upload_limited
            saves = []
            async def paused_save(file, path, limit):
                saves.append(path)
                if len(saves) == 1:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(.005)
                return await original(file, path, limit)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', 10, create=True), \
                 patch.object(script, '_save_upload_limited', side_effect=paused_save):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    first = asyncio.create_task(client.post('/api/upload', files={'file': ('one.txt', b'123456')}))
                    second = None
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        second = asyncio.create_task(client.post('/api/upload', files={'file': ('two.md', b'abcdef')}))
                        await asyncio.sleep(.03)
                        self.assertEqual({'ok': True}, (await client.get('/fixture/ping')).json())
                        release.set()
                        results = await asyncio.gather(first, second)
                        self.assertEqual([200, 413], sorted(result.status_code for result in results))
                        files = list(uploads.iterdir())
                        self.assertEqual(1, len(files))
                        self.assertEqual(6, sum(path.stat().st_size for path in files))
                    finally:
                        release.set()
                        await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)

    async def test_epub_expansion_refusal_removes_only_its_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            existing = uploads / 'existing.txt'
            existing.write_bytes(b'old source')
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', 32, create=True), \
                 patch.object(script, 'extract_epub_text', return_value='あ' * 20):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    response = await client.post('/api/upload', files={'file': ('book.EPUB', b'fixture archive')})
                    self.assertEqual(413, response.status_code, response.text)
                    self.assertEqual([existing], list(uploads.iterdir()))
                    self.assertEqual(b'old source', existing.read_bytes())
                    self.assertFalse((Path(tmp) / 'state.json').exists())

    async def test_cancelled_waiter_settles_lock_admission_and_releases_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            source = UploadFile(filename='cancelled.txt', file=io.BytesIO(b'cancelled content'))
            lease = script.file_lock(str(uploads) + '.quota')
            lease.__enter__()
            task = None
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp):
                try:
                    task = asyncio.create_task(script.upload_file(source))
                    await asyncio.sleep(.03)
                    task.cancel()
                    await asyncio.sleep(.01)
                    task.cancel()
                    await asyncio.sleep(.01)
                    self.assertFalse(task.done(), 'cancelled admission abandoned its live lock worker')
                finally:
                    lease.__exit__(None, None, None)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                with script.file_lock(str(uploads) + '.quota', timeout=.1):
                    pass
                self.assertEqual([], list(uploads.iterdir()))
                self.assertFalse((Path(tmp) / 'state.json').exists())
                await source.close()

    async def test_cancelled_path_claim_finishes_then_removes_its_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            source = UploadFile(filename='cancelled.txt', file=io.BytesIO(b'cancelled content'))
            entered, release = threading.Event(), threading.Event()
            original = script._claim_unique_path
            def paused_claim(directory, filename):
                path = original(directory, filename)
                entered.set()
                if not release.wait(2):
                    raise AssertionError('path-claim fixture was not released')
                return path
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, '_claim_unique_path', side_effect=paused_claim):
                task = asyncio.create_task(script.upload_file(source))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertEqual([], list(uploads.iterdir()))
                self.assertFalse((Path(tmp) / 'state.json').exists())
                with script.file_lock(str(uploads) + '.quota', timeout=.1):
                    pass
                await source.close()

    async def test_exact_aggregate_boundary_and_existing_individual_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            (uploads / 'old.txt').write_bytes(b'old!')
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', 10):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    response = await client.post('/api/upload', files={'file': ('new.MD', b'123456')})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(10, sum(path.stat().st_size for path in uploads.iterdir()))
                    response = await client.post('/api/upload', files={'file': ('one_more.txt', b'x')})
                    self.assertEqual(413, response.status_code)
                    self.assertFalse((uploads / 'one_more.txt').exists())
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', 100), \
                 patch.object(script, 'MAX_SCRIPT_UPLOAD_BYTES', 4):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    response = await client.post('/api/upload', files={'file': ('oversized.txt', b'12345')})
                    self.assertEqual(413, response.status_code)
                    self.assertFalse((uploads / 'oversized.txt').exists())
                    self.assertEqual(b'old!', (uploads / 'old.txt').read_bytes())

    async def test_busy_storage_returns_retryable_status_then_admits_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp) / 'uploads'
            uploads.mkdir()
            original = script.file_lock
            held = original(str(uploads) + '.quota')
            held.__enter__()
            def short_lock(path):
                return original(path, timeout=.02)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', tmp), \
                 patch.object(script, 'file_lock', side_effect=short_lock):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
                    try:
                        response = await client.post('/api/upload', files={'file': ('book.txt', b'content')})
                        self.assertEqual(503, response.status_code, response.text)
                        self.assertIn('retry', response.json()['detail'])
                        self.assertEqual([], list(uploads.iterdir()))
                    finally:
                        held.__exit__(None, None, None)
                    response = await client.post('/api/upload', files={'file': ('book.txt', b'content')})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(b'content', (uploads / 'book.txt').read_bytes())
