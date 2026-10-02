"""Config lock contention must not stop other HTTP work on the same event loop."""
from tests.test_support import assert_file_lock_released
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from routers import system
from utils import file_lock


class ConfigSaveConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_lock_wait_leaves_same_event_loop_http_requests_responsive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            profile = {"base_url":"http://localhost:1234/v1", "api_key":"stored-secret", "model_name":"model"}
            path.write_text(json.dumps({"llm":profile, "llm_local":profile, "llm_mode":"local",
                "tts":{"mode":"local"}, "generation":{"three_pass_chunk_size":9000}}))
            before = path.read_bytes()
            incoming = {**profile, "api_key":"[REDACTED]"}
            payload = {"llm":incoming, "llm_local":incoming, "llm_mode":"local", "tts":{"mode":"local"},
                       "generation":{"chunk_size":2500}}
            original_payload = json.dumps(payload, sort_keys=True)
            lock_held, attempted, release = threading.Event(), threading.Event(), threading.Event()
            errors, writer_threads = [], []
            event_loop_thread = threading.get_ident()

            def holder():
                try:
                    with file_lock(str(path)):
                        lock_held.set()
                        if not release.wait(5):
                            raise AssertionError('Config fixture lock was never released')
                except BaseException as error:
                    errors.append(error)

            def watchdog():
                # Release even when the buggy handler blocks the loop; never leak a holder thread.
                if attempted.wait(3):
                    release.wait(1)
                release.set()

            @contextmanager
            def observed_lock(destination):
                writer_threads.append(threading.get_ident())
                attempted.set()
                with file_lock(destination):
                    yield

            holder_thread = threading.Thread(target=holder)
            watchdog_thread = threading.Thread(target=watchdog)
            holder_thread.start()
            self.assertTrue(lock_held.wait(3))
            watchdog_thread.start()
            app = FastAPI()
            app.include_router(system.router)
            @app.get('/fixture/ping')
            async def ping():
                return {'ok':True}
            task = None
            engine = object()
            try:
                with patch.object(system, "CONFIG_PATH", str(path)), \
                     patch.object(system, "file_lock", side_effect=observed_lock), \
                     patch.object(system.project_manager, "invalidate_config_cache") as invalidate, \
                     patch.object(system.project_manager, "engine", engine):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                        task = asyncio.create_task(client.post('/api/config', json=payload))
                        self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                        self.assertFalse(task.done(), 'The handler blocked the event loop until the holder released')
                        response = await client.get('/fixture/ping')
                        self.assertEqual(200, response.status_code)
                        self.assertEqual({'ok':True}, response.json())
                        self.assertFalse(release.is_set(), 'The independent request only ran after lock release')
                        self.assertNotEqual(event_loop_thread, writer_threads[0])
                        self.assertEqual(before, path.read_bytes())
                        self.assertIs(engine, system.project_manager.engine)
                        invalidate.assert_not_called()
                        release.set()
                        saved_response = await task
                        self.assertEqual(200, saved_response.status_code, saved_response.text)
                        self.assertEqual({'status':'saved'}, saved_response.json())
                        invalidate.assert_called_once_with()
                        self.assertIsNone(system.project_manager.engine)
            finally:
                release.set()
                if task is not None and not task.done():
                    await task
                holder_thread.join(5)
                watchdog_thread.join(5)
            self.assertFalse(holder_thread.is_alive())
            self.assertFalse(watchdog_thread.is_alive())
            self.assertEqual([], errors)
            saved = json.loads(path.read_text())
            self.assertEqual(2500, saved['generation']['chunk_size'])
            self.assertEqual(9000, saved['generation']['three_pass_chunk_size'])
            self.assertEqual('stored-secret', saved['llm_local']['api_key'])
            self.assertEqual('stored-secret', saved['llm']['api_key'])
            assert_file_lock_released(str(path))
            self.assertEqual(original_payload, json.dumps(payload, sort_keys=True))

    async def test_publication_error_preserves_bytes_cache_and_lock_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            profile = {'base_url':'http://localhost:1234/v1', 'api_key':'local', 'model_name':'model'}
            path.write_text(json.dumps({'llm':profile, 'llm_local':profile, 'llm_mode':'local', 'tts':{'mode':'local'}}))
            before = path.read_bytes()
            app = FastAPI()
            app.include_router(system.router)
            engine = object()
            with patch.object(system, 'CONFIG_PATH', str(path)), \
                 patch.object(system, 'atomic_json_write', side_effect=PermissionError('fixture publication denied')), \
                 patch.object(system.project_manager, 'invalidate_config_cache') as invalidate, \
                 patch.object(system.project_manager, 'engine', engine):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://fixture') as client:
                    response = await client.post('/api/config', json={'llm':profile, 'llm_local':profile, 'llm_mode':'local', 'tts':{'mode':'local'}})
                self.assertEqual(500, response.status_code)
                invalidate.assert_not_called()
                self.assertIs(engine, system.project_manager.engine)
            self.assertEqual(before, path.read_bytes())
            assert_file_lock_released(str(path))


class ConfigEngineReloadTests(unittest.TestCase):
    def test_actual_setup_save_recreates_engine_from_new_tts_config_even_with_same_mtime(self):
        import copy
        from types import SimpleNamespace
        from fastapi.testclient import TestClient
        import project
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            profile = {'base_url':'http://localhost:1234/v1','api_key':'local','model_name':'fixture'}
            path.write_text(json.dumps({'llm':profile,'llm_local':profile,'llm_mode':'local',
                                        'tts':{'mode':'local','language':'English','parallel_workers':1,
                                               'max_new_tokens':2048}}))
            manager = project.ProjectManager(tmp)
            manager.config_path = str(path)
            snapshots = []
            def make_engine(config):
                snapshot = copy.deepcopy(config)
                snapshots.append(snapshot)
                return SimpleNamespace(mode=snapshot['tts']['mode'],config=snapshot)
            app = FastAPI()
            app.include_router(system.router)
            new_tts = {'mode':'external','url':'http://127.0.0.1:7861',
                       'external_urls':['http://127.0.0.1:7861','http://127.0.0.1:7862'],
                       'language':'Japanese','max_new_tokens':4096,'parallel_workers':4}
            payload = {'llm':profile,'llm_local':profile,'llm_mode':'local','tts':new_tts}
            before_payload = copy.deepcopy(payload)
            with patch.object(project.os.path,'getmtime',return_value=123), \
                 patch.object(project,'TTSEngine',side_effect=make_engine), \
                 patch.object(system,'CONFIG_PATH',str(path)), \
                 patch.object(system,'project_manager',manager), \
                 TestClient(app) as client:
                original_engine = manager.get_engine()
                self.assertEqual('local',original_engine.mode)
                response = client.post('/api/config',json=payload)
                self.assertEqual(200,response.status_code,response.text)
                self.assertIsNone(manager.engine)
                self.assertIsNone(manager._config_cache)
                recreated = manager.get_engine()
                self.assertIsNot(original_engine,recreated)
                self.assertIs(recreated,manager.get_engine())
            stored = json.loads(path.read_text())
            self.assertEqual(2,len(snapshots))
            self.assertEqual(stored['tts'],snapshots[-1]['tts'])
            for key,value in new_tts.items():
                self.assertEqual(value,recreated.config['tts'][key])
            self.assertEqual('external',recreated.mode)
            self.assertEqual(before_payload,payload)
            assert_file_lock_released(str(path))
