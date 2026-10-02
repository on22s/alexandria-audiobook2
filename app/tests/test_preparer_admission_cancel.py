import asyncio
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
import core
from routers import preparer


class PreparerAdmissionCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.uploads = self.root / 'uploads'; self.uploads.mkdir()
        self.outputs = self.root / 'outputs'; self.outputs.mkdir()
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values(): row['running'] = False
        for context in (patch.object(core, 'process_state', self.state), patch.object(preparer, 'process_state', self.state),
                        patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}),
                        patch.object(core, 'DATA_DIR', str(self.root)), patch.object(core, 'acquire_gpu_lock', return_value=None),
                        patch.object(core, 'llm_is_on_this_gpu', return_value=True),
                        patch.object(preparer, 'UPLOADS_DIR', str(self.uploads)), patch.object(preparer, 'PREPARER_OUTPUT_DIR', str(self.outputs)),
                        patch.object(preparer, '_resolve_preparer_interpreter', return_value=sys.executable),
                        patch.object(preparer, '_revalidate_voicelab_paths', return_value=None),
                        patch.object(preparer, 'check_disk_space', return_value=(True, 100)),
                        patch.object(preparer, '_run_claimed_background_task', side_effect=lambda name, run: run()),
                        patch.object(preparer, '_stream_subprocess_to_logs', return_value=(0, []))):
            context.start(); self.addCleanup(context.stop)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name, owner in list(core._task_claims.items()): core.release_gpu_task_claim(name, owner['id'])

    async def start(self, name, tasks):
        upload = UploadFile(filename='book.wav', file=io.BytesIO(b'new upload bytes'))
        if name == 'preparer':
            return await preparer.preparer_start(tasks, json.dumps({'audio_filename': 'book.wav'}), upload, None)
        return await preparer.preparer_batch_upload_start(tasks,
            json.dumps({'tasks': [{'audio_filename': 'book.wav', 'output_filename': 'dataset.zip'}]}), [upload])

    def test_both_upload_starts_reserve_before_first_await_and_adopt_same_owner(self):
        for name in ('preparer', 'batch_preparer'):
            with self.subTest(task=name):
                arrived, release = asyncio.Event(), asyncio.Event()
                tasks = BackgroundTasks(); old = self.uploads / 'book.wav'; old.write_bytes(b'prior upload')
                original = preparer._save_upload_limited
                async def save(*args):
                    arrived.set(); await release.wait(); return await original(*args)
                async def run():
                    pending = asyncio.create_task(self.start(name, tasks))
                    await asyncio.wait_for(arrived.wait(), 2)
                    try:
                        owner = core._task_claims.get(name)
                        self.assertIsNotNone(owner, 'upload started without a task reservation')
                        token = owner['id']; self.assertEqual('pending', owner['phase'])
                        with self.assertRaises(HTTPException): core.claim_gpu_task('audio')
                        self.assertEqual(b'prior upload', old.read_bytes())
                    finally:
                        release.set(); response = await pending
                    self.assertEqual('started', response['status'])
                    self.assertEqual(token, core._task_claims[name]['id'])
                    self.assertEqual(1, len(tasks.tasks))
                    self.assertEqual(b'new upload bytes', old.read_bytes())
                    await tasks()
                    self.assertNotIn(name, core._task_claims)
                    self.assertFalse(self.state[name]['running'])
                try:
                    with patch.object(preparer, '_save_upload_limited', side_effect=save): asyncio.run(run())
                finally:
                    self.release_claims()
                self.assertEqual(['book.wav'], [p.name for p in self.uploads.iterdir()])

    def test_upload_failure_or_request_cancel_releases_pending_owner_and_partial_stages(self):
        for name in ('preparer', 'batch_preparer'):
            for mode in ('failure', 'cancel'):
                with self.subTest(task=name, mode=mode):
                    # Keep baseline cases independent after a deliberately leaked old-code stage.
                    for leftover in self.uploads.glob('*.upload.*'): leftover.unlink()
                    old = self.uploads / 'book.wav'; old.write_bytes(b'prior upload')
                    arrived = asyncio.Event(); tasks = BackgroundTasks()
                    async def save(upload, path, limit):
                        Path(path).write_bytes(b'partial stage'); arrived.set()
                        if mode == 'failure': raise HTTPException(413, 'fixture too large')
                        await asyncio.Event().wait()
                    async def run():
                        pending = asyncio.create_task(self.start(name, tasks))
                        await asyncio.wait_for(arrived.wait(), 2)
                        if mode == 'cancel': pending.cancel()
                        with self.assertRaises(asyncio.CancelledError if mode == 'cancel' else HTTPException): await pending
                        self.assertNotIn(name, core._task_claims)
                        self.assertFalse(self.state[name]['running'])
                    with patch.object(preparer, '_save_upload_limited', side_effect=save): asyncio.run(run())
                    self.assertEqual(b'prior upload', old.read_bytes())
                    self.assertEqual(['book.wav'], [p.name for p in self.uploads.iterdir()])
                    self.assertEqual([], tasks.tasks)

    def test_cancel_rejects_idle_and_signals_only_active_disposable_child(self):
        for name, cancel in (('preparer', preparer.preparer_cancel), ('batch_preparer', preparer.preparer_batch_cancel)):
            with self.subTest(task=name):
                self.state[name]['cancel'] = False
                with self.assertRaises(HTTPException) as error: asyncio.run(cancel())
                self.assertEqual(400, error.exception.status_code)
                self.assertFalse(self.state[name]['cancel'])
            with self.subTest(task=name, phase='active child'):
                proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
                try:
                    self.state[name].update(running=True, process=proc)
                    with patch.object(preparer, '_send_signal_tree', wraps=preparer._send_signal_tree) as signal_tree:
                        self.assertEqual({'status': 'cancel_requested'}, asyncio.run(cancel()))
                        proc.wait(timeout=3)
                        self.assertNotEqual(0, proc.returncode)
                        self.assertTrue(self.state[name]['cancel'])
                        signal_tree.assert_called_once()
                        self.assertEqual(proc, signal_tree.call_args.args[0])
                        asyncio.run(cancel())  # Already-exited child needs no signal.
                        signal_tree.assert_called_once()
                finally:
                    if proc.poll() is None: proc.terminate(); proc.wait(timeout=3)
                    self.state[name].update(running=False, process=None)
