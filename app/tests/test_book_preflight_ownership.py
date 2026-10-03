"""A real CPU child retains Script admission until cancellation reaps it."""
import asyncio
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fastapi import BackgroundTasks, HTTPException
import core
from routers import script

class BookPreflightOwnershipTests(unittest.TestCase):
    def test_real_child_cancel_keeps_claim_until_child_reaped(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); source = root / 'book.txt'; source.write_text('Source text.')
            config = root / 'config.json'; config.write_text('{}')
            (root / 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
            states = copy.deepcopy(core.process_state)
            for state in states.values():
                state['running'] = False
            for module, name, value in [(core, 'process_state', states), (script, 'process_state', states),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {}), (core, 'DATA_DIR', tmp),
                    (script, 'DATA_DIR', tmp), (script, 'CONFIG_PATH', str(config)),
                    (core, 'RUN_HISTORY_DIR', str(root / 'run_history'))]:
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            stack.enter_context(patch.object(script, 'get_active_book_id', return_value='book'))
            stack.enter_context(patch.object(script, 'build_generate_script_command', return_value=[
                sys.executable, '-u', '-c', 'import os,time;print("ready",flush=True);time.sleep(30)']))
            entered = threading.Event(); release = threading.Event(); seen = {}; errors = []
            native_cancel = core.apply_cancel_escalation
            def cancel(process, requested, killed):
                seen['process'] = process
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Cancellation test gate was not released')
                return native_cancel(process, requested, killed)
            stack.enter_context(patch.object(core, 'apply_cancel_escalation', side_effect=cancel))
            tasks = BackgroundTasks()
            receipt = script.start_book_preflight(tasks, None)
            self.assertTrue(core.is_task_running('script'))
            def worker():
                try:
                    asyncio.run(tasks())
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=worker); thread.start()
            try:
                # Native cancellation code is reached only after real subprocess creation.
                deadline = time.monotonic() + 5
                while 'ready' not in states['script'].get('logs', []) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn('ready', states['script'].get('logs', []), errors)
                asyncio.run(script.book_preflight_cancel(receipt['job_id']))
                self.assertTrue(entered.wait(5))
                process = seen['process']
                self.assertIsNone(process.poll())
                self.assertEqual(receipt['claim_id'], core._task_claims['script']['id'])
                self.assertTrue(core.is_task_running('script'))
                with self.assertRaises(HTTPException):
                    core.claim_gpu_task('audio')
            finally:
                states['script']['cancel'] = True
                release.set(); thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertIsNotNone(seen['process'].poll())
            self.assertFalse(core.is_task_running('script'))
            self.assertNotIn('script', core._task_claims)
            final = script.get_book_preflight_result(receipt['job_id'])
            self.assertEqual('cancelled', final['status'])
            self.assertNotIn('summary', final)
            self.assertEqual([], list(root.glob('script_preflight_*')))

    def test_failed_registration_or_receipt_publish_releases_pending_claim(self):
        class RejectTasks(BackgroundTasks):
            def add_task(self, *args, **kwargs):
                raise RuntimeError('registration rejected')
        for mode in ('registration', 'receipt'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp); source = root / 'book.txt'; source.write_text('Source.')
                config = root / 'config.json'; config.write_text('{}')
                (root / 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
                previous = root / 'script_preflight.json'; previous.write_bytes(b'previous receipt')
                states = copy.deepcopy(core.process_state)
                for state in states.values():
                    state['running'] = False
                for module, name, value in [(core, 'process_state', states), (script, 'process_state', states),
                        (core, '_task_claims', {}), (core, '_gpu_leases', {}), (core, 'DATA_DIR', tmp),
                        (script, 'DATA_DIR', tmp), (script, 'CONFIG_PATH', str(config))]:
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
                stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
                stack.enter_context(patch.object(script, 'get_active_book_id', return_value='book'))
                stream = stack.enter_context(patch.object(script, '_stream_subprocess_to_logs'))
                tasks = RejectTasks() if mode == 'registration' else BackgroundTasks()
                native_write = script.atomic_json_write
                def write(value, path, *args, **kwargs):
                    if mode == 'receipt' and path == str(previous):
                        raise OSError('receipt publication refused')
                    return native_write(value, path, *args, **kwargs)
                stack.enter_context(patch.object(script, 'atomic_json_write', side_effect=write))
                with self.assertRaisesRegex((RuntimeError, OSError), 'rejected|refused'):
                    script.start_book_preflight(tasks, None)
                self.assertFalse(core.is_task_running('script'))
                self.assertEqual({}, core._task_claims)
                asyncio.run(tasks())
                stream.assert_not_called()
                self.assertEqual(b'previous receipt', previous.read_bytes())
                self.assertEqual([], list(root.glob('script_preflight_*')))
