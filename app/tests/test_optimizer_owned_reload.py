"""Native CPU CLI reload and ASGI cancellation must retain GPU admission."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import threading
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx
import core
import lmstudio_settings as settings
from experiments.gpu_guard import acquire_gpu_lock, gpu_is_busy
from routers import system


class OptimizerOwnershipTests(unittest.TestCase):
    def test_reload_success_does_not_hide_lost_endpoint_verification(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state, changes, release, lock, app = self.get_fixture(tmp, stack)
            release.write_text('CPU fixture proceeds')
            stack.enter_context(patch.object(settings, 'get_lmstudio_endpoint_status',
                side_effect=[{'runtime': 'lmstudio'}, None]))
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/api/lmstudio/optimize', json={'enable': False})
                self.assertEqual(502, response.status_code)
                self.assertIn('fresh endpoint status', response.json()['detail'])
                operations = [json.loads(line) for line in changes.read_text().splitlines()]
                self.assertEqual(1, sum(args[0] == 'load' for args in operations))
                self.assertFalse(state['lmstudio_optimize']['running'])
            asyncio.run(run())

    def test_unknown_endpoint_is_rejected_before_any_cli_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state, changes, release, lock, app = self.get_fixture(tmp, stack)
            stack.enter_context(patch.object(settings, 'get_lmstudio_endpoint_status', return_value=None))
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/api/lmstudio/optimize', json={'enable': False})
                self.assertEqual(400, response.status_code)
                self.assertIn('not applied', response.json()['detail'])
                self.assertFalse(changes.exists())
                self.assertFalse(state['lmstudio_optimize']['running'])
            asyncio.run(run())

    def get_fixture(self, directory, stack):
        root = Path(directory)
        changes, release, lock = root/'changes.jsonl', root/'release', root/'gpu.lock'
        cli = root/'lms'
        cli.write_text('#!'+sys.executable+'\n'+
            'import json,pathlib,sys,time\n'+
            'changes=pathlib.Path('+repr(str(changes))+')\n'+
            'with changes.open("a") as output:output.write(json.dumps(sys.argv[1:])+"\\n")\n'+
            'release=pathlib.Path('+repr(str(release))+')\n'+
            'if sys.argv[1]=="load":\n'+
            ' while not release.exists():time.sleep(.01)\n')
        cli.chmod(0o755)
        state = copy.deepcopy(core.process_state)
        for value in state.values():
            value['running'] = False
        for owner, name, value in ((core, 'process_state', state), (system, 'process_state', state),
                                   (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            stack.enter_context(patch.object(owner, name, value))
        stack.enter_context(patch.object(core, 'acquire_gpu_lock', side_effect=lambda: acquire_gpu_lock(str(lock))))
        stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
        stack.enter_context(patch.object(settings, 'find_lms_binary', return_value=str(cli)))
        stack.enter_context(patch.object(system, 'load_app_config', return_value={
            'llm_mode': 'local', 'llm': {'base_url': 'http://localhost:1234/v1', 'model_name': 'fixture'}}))
        stack.enter_context(patch.object(system, 'get_lmstudio_status', return_value={'loaded': True}))
        stack.enter_context(patch.object(settings, 'get_llama_cpp_status', return_value=None))
        stack.enter_context(patch.object(settings, 'get_lmstudio_endpoint_status', return_value={'runtime': 'lmstudio'}))
        stack.enter_context(patch.object(settings, 'get_lmstudio_management_binding', return_value=(True, 1234, 'fixture verified')))
        stack.enter_context(patch.object(settings, 'get_lmstudio_status', return_value={'loaded': True, 'optimized': False}))
        stack.enter_context(patch.object(settings, 'get_local_vram_bytes', return_value=None))
        app = FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(system.router)
        return state, changes, release, lock, app

    def test_busy_review_is_rejected_before_any_cli_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state, changes, release, lock, app = self.get_fixture(tmp, stack)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    state['review']['running'] = True
                    response = await client.post('/api/lmstudio/optimize',json={'enable': False})
                    self.assertEqual(400,response.status_code)
                    self.assertFalse(changes.exists())
                    state['review']['running'] = False
                    release.write_text('continue')
                    response = await client.post('/api/lmstudio/optimize',json={'enable': False})
                    self.assertEqual(200,response.status_code,response.text)
                    recorded = [json.loads(line) for line in changes.read_text().splitlines()]
                    self.assertEqual(['unload','load'],[row[0] for row in recorded])
                    self.assertFalse(state['lmstudio_optimize']['running'])
                    core.release_gpu_task_claim('lmstudio_optimize')
            asyncio.run(run())

    def test_cancelled_request_keeps_reload_owned_until_native_cli_exits(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state, changes, release, lock, app = self.get_fixture(tmp, stack)
            async def wait_for(predicate):
                deadline = asyncio.get_running_loop().time()+4
                while asyncio.get_running_loop().time()<deadline:
                    if predicate():
                        return
                    await asyncio.sleep(.01)
                self.fail('native reload did not reach the expected state')
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    task = asyncio.create_task(client.post('/api/lmstudio/optimize',json={'enable': False}))
                    try:
                        await wait_for(lambda: changes.exists() and len(changes.read_text().splitlines())==2)
                        self.assertTrue(gpu_is_busy(str(lock)))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertTrue(state['lmstudio_optimize']['running'], 'cancelled HTTP scope released an active reload')
                        self.assertTrue(core.is_task_running('lmstudio_optimize'))
                        self.assertTrue(gpu_is_busy(str(lock)))
                        with self.assertRaises(HTTPException):
                            core.claim_gpu_task('audio')
                        self.assertFalse(state['audio']['running'])
                        release.write_text('continue')
                        await wait_for(lambda: not core.is_task_running('lmstudio_optimize') and not gpu_is_busy(str(lock)))
                        self.assertNotIn('lmstudio_optimize',core._task_claims)
                        claim = core.claim_gpu_task('audio')
                        core.release_gpu_task_claim('audio',claim)
                    finally:
                        release.write_text('fixture cleanup')
                        if not task.done():
                            await task
                        await wait_for(lambda: not gpu_is_busy(str(lock)))
            asyncio.run(run())

    def test_cancellation_before_worker_start_releases_pending_reservation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state, changes, release, lock, app = self.get_fixture(tmp, stack)
            hold, entered = threading.Event(), threading.Event()
            def occupy_worker():
                entered.set()
                hold.wait(timeout=10)
            async def run():
                loop = asyncio.get_running_loop()
                loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
                blocker = loop.run_in_executor(None, occupy_worker)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    task = None
                    try:
                        while not entered.is_set():
                            await asyncio.sleep(.01)
                        task = asyncio.create_task(client.post('/api/lmstudio/optimize',json={'enable': False}))
                        deadline = loop.time()+3
                        while not core.is_task_running('lmstudio_optimize') and loop.time()<deadline:
                            await asyncio.sleep(.01)
                        self.assertEqual('pending',core._task_claims['lmstudio_optimize']['phase'])
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertNotIn('lmstudio_optimize',core._task_claims)
                        self.assertFalse(state['lmstudio_optimize']['running'])
                        self.assertFalse(gpu_is_busy(str(lock)))
                        hold.set()
                        await blocker
                        await asyncio.sleep(.1)
                        self.assertFalse(changes.exists(), 'cancelled queued reload mutated the model')
                    finally:
                        release.write_text('fixture cleanup')
                        hold.set()
                        if task is not None and not task.done():
                            await task
                        await blocker
            asyncio.run(run())
