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

    async def require_responsive_lock_wait(self, method, while_held=None):
        acquired, entered, release = threading.Event(), threading.Event(), threading.Event()
        threads = []

        def hold_lock():
            with file_lock(str(self.cast_path)):
                acquired.set()
                release.wait(10)
                if while_held:
                    while_held()

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
        # A build writes while holding the lock the save is waiting for. The
        # save must carry that provenance forward, not one read before waiting.
        def build_writes():  # the build's atomic write, landing while it holds the lock
            atomic_json_write({'cast': CAST, 'provenance': {'model': 'written-by-build'}}, str(self.cast_path))

        result = await self.require_responsive_lock_wait('POST', while_held=build_writes)
        self.assertEqual({'status': 'saved', 'count': 1}, result)
        document = json.loads(self.cast_path.read_text(encoding='utf-8'))
        self.assertEqual(CAST, document['cast'])
        self.assertTrue(document['provenance']['edited'])
        self.assertEqual('written-by-build', document['provenance'].get('model'))
        # The build path itself now takes the same lock the routes use.
        held, attempted = threading.Event(), threading.Event()

        @contextmanager
        def observed_build_lock(path, *args, **kwargs):
            attempted.set()
            with file_lock(path, *args, **kwargs):
                yield

        with patch('cast_list.file_lock', observed_build_lock):
            writer = threading.Thread(target=lambda: (save_cast_list(str(self.cast_path), CAST, {'model': 'b'}),
                                                      held.set()))
            try:
                with file_lock(str(self.cast_path)):
                    before_write = self.cast_path.read_bytes()
                    writer.start()
                    self.assertTrue(attempted.wait(3), 'save_cast_list did not attempt the shared lock')
                    self.assertFalse(held.wait(.3), 'save_cast_list wrote while another holder had the lock')
                    self.assertEqual(before_write, self.cast_path.read_bytes())
            finally:
                writer.join(3)
        self.assertFalse(writer.is_alive())
        self.assertTrue(held.is_set())

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

    async def test_build_snapshots_shared_saved_controls_and_source_digest(self):
        config = {'generation': {'cast_list_incremental': True, 'cast_list_source_chars': 1234, 'cast_list_max_tokens': 12000}}
        with patch.object(script, 'load_app_config', return_value=config), patch.object(script, 'schedule_claimed_background_task') as schedule:
            response = await self.client.post('/api/cast_list/build', json={'strip_front_matter': False})
        self.assertEqual(200, response.status_code, response.text)
        command = schedule.call_args.args[3]
        self.assertIn('--incremental', command)
        self.assertIn('--no-strip-front-matter', command)
        self.assertEqual('1234', command[command.index('--source-chars') + 1])
        self.assertEqual('12000', command[command.index('--max-tokens') + 1])
        self.assertEqual(self.cast_path.stem, command[command.index('--source-sha256') + 1])
        with patch.object(script, 'load_app_config', return_value=config), patch.object(script, 'schedule_claimed_background_task') as schedule:
            response = await self.client.post('/api/cast_list/build', json={'incremental': False, 'max_tokens': 16000})
        self.assertEqual(200, response.status_code)
        self.assertIn('--no-incremental', schedule.call_args.args[3])
        self.assertIn('16000', schedule.call_args.args[3])

    async def test_build_rejects_invalid_controls_before_scheduling(self):
        for body in ({'incremental': 'true'}, {'source_chars': True}, {'source_chars': 0}, {'source_chars': 1200001}, {'max_tokens': 0}, {'max_tokens': 1.5}):
            with self.subTest(body=body), patch.object(script, 'schedule_claimed_background_task') as schedule:
                response = await self.client.post('/api/cast_list/build', json=body)
                self.assertEqual(422, response.status_code, response.text)
                schedule.assert_not_called()

    async def test_generation_command_carries_batch_alias_override_and_preflight_cast_identity(self):
        command = script.build_generate_script_command(str(self.root / 'book.txt'), cast_file=str(self.cast_path), cast_aliases=True)
        self.assertIn('--cast-aliases', command)
        settings = {'cast_aliases': True}
        first = script.get_cast_sizing_settings(settings, str(self.root / 'book.txt'))
        self.assertEqual([['ALICE']], first['cast']['alias_groups'])
        save_cast_list(str(self.cast_path), [{'name': 'ALICE', 'aliases': ['THE PILOT']}])
        second = script.get_cast_sizing_settings(settings, str(self.root / 'book.txt'))
        self.assertNotEqual(first['cast']['sha256'], second['cast']['sha256'])
        self.assertEqual({}, script.get_cast_sizing_settings({}, str(self.root / 'book.txt')))

    async def test_batch_runs_cast_before_generation_and_keeps_failure_cancel_fences(self):
        source = str(self.root / 'book.txt')
        output = str(self.root / 'script.json')
        settings = {'incremental': True, 'source_chars': 1234, 'max_tokens': 12000}
        job = {'index': 0, 'filename': 'book.txt', 'input_path': source, 'output_path': output,
               'safe_stem': 'script', 'build_cast_list': True, 'cast_list_settings': settings}
        for cast_rc, cancel in ((0, False), (1, False), (0, True)):
            with self.subTest(cast_rc=cast_rc, cancel=cancel):
                state = {'tasks': [{}], 'logs': [], 'cancel': False}
                commands = []
                def run(command, *args, **kwargs):
                    commands.append(command)
                    if len(commands) == 1:
                        state['cancel'] = cancel
                        return cast_rc, None
                    return 0, None
                with patch.object(script, 'get_existing_cast_list', side_effect=[None, str(self.cast_path)]), patch.object(script, '_stream_subprocess_to_logs', side_effect=run), patch.object(script, 'get_active_reasoning_effort', return_value=None):
                    script._run_claimed_batch_script_job(job, state, str(self.root / 'log'), 1)
                self.assertIn('--incremental', commands[0])
                self.assertIn('1234', commands[0])
                self.assertIn('12000', commands[0])
                if cast_rc or cancel:
                    self.assertEqual(1, len(commands))
                    self.assertEqual('cancelled' if cancel else 'failed', state['tasks'][0]['status'])
                else:
                    self.assertEqual(2, len(commands))
                    self.assertIn('--cast-aliases', commands[1])
                    self.assertIn(str(self.cast_path), commands[1])
                    self.assertEqual('done', state['tasks'][0]['status'])
