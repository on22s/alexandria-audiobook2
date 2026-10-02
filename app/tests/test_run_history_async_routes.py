import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from routers import system
from run_history import get_run, list_runs, start_run


class RunHistoryAsyncRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_history_reads_allow_other_http_requests_to_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_id = start_run(tmp, 'review')
            application = FastAPI()
            application.include_router(system.router)
            @application.get('/fixture-ping')
            async def ping():
                return {'ready': True}
            main_thread = threading.get_ident()
            for function, url, reader in (
                    ('list_runs', '/api/runs', list_runs),
                    ('get_run', '/api/runs/'+run_id, get_run)):
                with self.subTest(function=function):
                    release = threading.Event()
                    entered = threading.Event()
                    threads = []
                    def slow_read(*args, **kwargs):
                        threads.append(threading.get_ident())
                        entered.set()
                        release.wait(1)
                        return reader(*args, **kwargs)
                    timer = threading.Timer(1, release.set)
                    timer.start()
                    try:
                        with patch.object(system, 'RUN_HISTORY_DIR', tmp), patch.object(system, function, side_effect=slow_read):
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://fixture') as client:
                                request = asyncio.create_task(client.get(url))
                                try:
                                    await asyncio.sleep(.02)
                                    self.assertTrue(entered.is_set())
                                    response = await asyncio.wait_for(client.get('/fixture-ping'), .3)
                                    self.assertEqual({'ready': True}, response.json())
                                    self.assertFalse(request.done(), 'history disk read blocked the event loop')
                                    self.assertNotEqual(main_thread, threads[0])
                                finally:
                                    release.set()
                                    result = await request
                                self.assertEqual(200, result.status_code)
                                expected = {'runs': list_runs(tmp)} if function=='list_runs' else get_run(tmp, run_id)
                                self.assertEqual(expected, result.json())
                    finally:
                        release.set()
                        timer.cancel()
                        timer.join()

    async def test_missing_run_retains_404_response(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(system, 'RUN_HISTORY_DIR', tmp):
            application = FastAPI()
            application.include_router(system.router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://fixture') as client:
                result = await client.get('/api/runs/missing')
            self.assertEqual(404, result.status_code)
            self.assertEqual({'detail': 'Run not found'}, result.json())
