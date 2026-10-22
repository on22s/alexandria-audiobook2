"""Real cast-file reads and lock waits must leave other HTTP requests responsive."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from cast_list import get_cast_list_path, save_cast_list
from routers import script
from utils import atomic_json_write, file_lock


CAST = [{'name': 'ALICE', 'aliases': []}]


class CastListAsyncRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = self.root / 'book.txt'
        source.write_text('A synthetic source for a cast-list scheduling test.', encoding='utf-8')
        atomic_json_write({'input_file_path': str(source)}, self.root / 'state.json')
        self.cast_path = Path(get_cast_list_path(str(source), str(self.root)))
        save_cast_list(str(self.cast_path), CAST)
        data_patch = patch.object(script, 'DATA_DIR', str(self.root))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        application = FastAPI()
        application.include_router(script.router)

        @application.get('/fixture-ping')
        async def ping():
            return {'ready': True}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                       base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)

    async def require_responsive_request(self, method, entered, release, threads):
        """A timer bounds a broken implementation without making a stall pass."""
        timer = threading.Timer(5, release.set)
        timer.start()
        request = asyncio.create_task(self.client.request(
            method, '/api/cast_list', **({'json': {'cast': CAST}} if method == 'POST' else {})))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            response = await asyncio.wait_for(self.client.get('/fixture-ping'), .5)
            self.assertEqual({'ready': True}, response.json())
            self.assertFalse(request.done(), 'cast-file work blocked other requests until it finished')
            self.assertTrue(threads)
            self.assertTrue(all(value != threading.get_ident() for value in threads))
        finally:
            release.set()
            timer.cancel()
            timer.join()
            result = await asyncio.wait_for(request, 3)
        self.assertEqual(200, result.status_code)
        return result.json()

    async def test_get_reads_cast_in_worker_and_preserves_response(self):
        entered, release, threads = threading.Event(), threading.Event(), []
        original = script.safe_load_json

        def read(path, *args, **kwargs):
            threads.append(threading.get_ident())
            if Path(path) == self.cast_path:
                entered.set()
                release.wait(10)
            return original(path, *args, **kwargs)

        with patch.object(script, 'safe_load_json', side_effect=read):
            result = await self.require_responsive_request('GET', entered, release, threads)
        self.assertEqual({'cast': CAST, 'count': 1, 'written': None, 'edited': False}, result)

    async def require_responsive_lock_wait(self, method):
        acquired, entered, release = threading.Event(), threading.Event(), threading.Event()
        threads = []

        def hold_lock():
            with file_lock(str(self.cast_path)):
                acquired.set()
                release.wait(10)

        @contextmanager
        def observed_lock(path, *args, **kwargs):
            threads.append(threading.get_ident())
            entered.set()
            with file_lock(path, *args, **kwargs):
                yield

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(await asyncio.to_thread(acquired.wait, 1))
            with patch.object(script, 'file_lock', observed_lock):
                return await self.require_responsive_request(method, entered, release, threads)
        finally:
            release.set()
            await asyncio.to_thread(holder.join, 3)
            self.assertFalse(holder.is_alive())

    async def test_save_waits_for_real_lock_without_blocking_ping(self):
        result = await self.require_responsive_lock_wait('POST')
        self.assertEqual({'status': 'saved', 'count': 1}, result)
        document = json.loads(self.cast_path.read_text(encoding='utf-8'))
        self.assertEqual(CAST, document['cast'])
        self.assertTrue(document['provenance']['edited'])

    async def test_delete_waits_for_real_lock_without_blocking_ping(self):
        self.assertEqual({'status': 'deleted'}, await self.require_responsive_lock_wait('DELETE'))
        self.assertFalse(self.cast_path.exists())

    async def test_refusals_and_missing_list_preserve_existing_behavior(self):
        previous = self.cast_path.read_bytes()
        bad = await self.client.post('/api/cast_list', json={'cast': [{'aliases': ['X']}]})
        self.assertEqual(400, bad.status_code)
        self.assertTrue(bad.json()['detail'].startswith('Cast list refused:'))
        self.assertEqual(previous, self.cast_path.read_bytes())
        self.cast_path.unlink()
        self.assertEqual({'cast': None}, (await self.client.get('/api/cast_list')).json())
        self.assertEqual({'status': 'deleted'}, (await self.client.delete('/api/cast_list')).json())
        atomic_json_write({}, self.root / 'state.json')
        for method in ('GET', 'POST', 'DELETE'):
            with self.subTest(method=method):
                result = await self.client.request(method, '/api/cast_list',
                    **({'json': {'cast': CAST}} if method == 'POST' else {}))
                self.assertEqual(400, result.status_code)
                self.assertEqual({'detail': 'No input file selected'}, result.json())
