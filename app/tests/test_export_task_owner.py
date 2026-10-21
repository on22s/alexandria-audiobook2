"""CPU exports retain exact task authority through final publication admission."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import core
import export_quality
from audio_generation_owner import (AudioGenerationCancelled, AudioGenerationOwner,
                                    ensure_audio_generation_publication, get_audio_generation_owner)
from routers import editor
from tests import test_export_task_results as tasks
from tests import test_m4b_export_quality as media


class ExportTaskOwnerTests(unittest.TestCase):
    def test_each_cpu_export_binds_exact_live_owner_and_releases_only_its_own_claim(self):
        for name in ('audacity_export', 'm4b_export', 'chapter_export'):
            for fault in ('cancel', 'replacement'):
                with self.subTest(name=name, fault=fault), ExitStack() as stack:
                    claim = 'a' * 32
                    states = {name: {'running': True, **({} if name == 'audacity_export' else {'cancel': False})}}
                    claims = {name: {'id': claim, 'phase': 'pending'}}
                    stack.enter_context(patch.object(core, 'process_state', states))
                    stack.enter_context(patch.object(core, '_task_claims', claims))
                    stack.enter_context(patch.object(core, '_gpu_leases', {}))
                    def run():
                        owner = get_audio_generation_owner()
                        self.assertIs(type(owner), AudioGenerationOwner)
                        self.assertEqual(claim, owner.claim_id)
                        with ensure_audio_generation_publication():
                            pass
                        with core._gpu_lock:
                            if fault == 'cancel':
                                states[name]['cancel'] = True
                            else:
                                claims[name] = {'id': 'b' * 32, 'phase': 'started'}
                        with self.assertRaises(AudioGenerationCancelled):
                            with ensure_audio_generation_publication():
                                self.fail('Stale or cancelled export authority was admitted')
                        return 'worker returned'
                    self.assertEqual('worker returned', core._run_owned_background_task(name, claim, run))
                    self.assertIsNone(get_audio_generation_owner())
                    if fault == 'cancel':
                        self.assertFalse(claims)
                        self.assertFalse(states[name]['running'])
                    else:
                        self.assertEqual('b' * 32, claims[name]['id'])
                        self.assertTrue(states[name]['running'])

    def test_native_http_cancel_after_real_media_check_keeps_claim_until_worker_finishes(self):
        with tasks.ExportTaskResultTests().fixture() as (root, states, client):
            manager = media.M4BExportQualityTests().fixture(root)
            output = Path(root) / 'audiobook.m4b'
            output.write_bytes(b'prior complete M4B')
            entered, release = threading.Event(), threading.Event()
            responses, seen = [], []
            admit = export_quality.get_admitted_export_report
            def held_admission(*args, **kwargs):
                result = admit(*args, **kwargs)
                owner = get_audio_generation_owner()
                self.assertIs(type(owner), AudioGenerationOwner)
                seen.append(owner.claim_id)
                entered.set()
                self.assertTrue(release.wait(5))
                return result
            with patch.object(editor, 'project_manager', manager), \
                    patch.object(export_quality, 'get_admitted_export_report', side_effect=held_admission):
                worker = threading.Thread(target=lambda: responses.append(client.post('/api/merge_m4b', json={})))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(seen[0], core._task_claims['m4b_export']['id'])
                    response = client.post('/api/merge_m4b/cancel')
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertTrue(states['m4b_export']['cancel'])
                    self.assertTrue(states['m4b_export']['running'])
                    self.assertEqual(seen[0], core._task_claims['m4b_export']['id'])
                finally:
                    release.set()
                    worker.join(10)
                self.assertFalse(worker.is_alive())
            self.assertEqual(200, responses[0].status_code)
            self.assertEqual({'status': 'cancelled', 'message': 'Export cancelled'},
                             states['m4b_export']['result'])
            self.assertFalse(states['m4b_export']['running'])
            self.assertFalse(core._task_claims)
            self.assertEqual(b'prior complete M4B', output.read_bytes())
            media.M4BExportQualityTests().assert_clean(root)

    def test_cancel_routes_set_flags_under_the_same_owner_mutex(self):
        for task, callback in [('m4b_export', editor.cancel_m4b_export),
                               ('chapter_export', editor.cancel_chapter_export)]:
            with self.subTest(task=task):
                state = {'running': True, 'cancel': False}
                entered, outcomes = threading.Event(), []
                mutex = core._gpu_lock
                class ObservedMutex:
                    def __enter__(self):
                        entered.set()
                        mutex.acquire()
                    def __exit__(self, *args):
                        mutex.release()
                with patch.dict(editor.process_state, {task: state}), \
                        patch.object(editor, '_gpu_lock', ObservedMutex()):
                    with mutex:
                        worker = threading.Thread(target=lambda: outcomes.append(asyncio.run(callback())))
                        worker.start()
                        self.assertTrue(entered.wait(3))
                        self.assertFalse(state['cancel'])
                        self.assertTrue(worker.is_alive())
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(state['cancel'])
                self.assertEqual([{'status': 'cancelling'}], outcomes)

    def test_cancel_mutex_wait_keeps_the_async_event_loop_responsive(self):
        for task, callback in [('m4b_export', editor.cancel_m4b_export),
                               ('chapter_export', editor.cancel_chapter_export)]:
            with self.subTest(task=task):
                state = {'running': True, 'cancel': False}
                held, release, entered, expired = (threading.Event() for _ in range(4))
                mutex = threading.Lock()
                class ObservedMutex:
                    def __enter__(self):
                        entered.set()
                        mutex.acquire()
                    def __exit__(self, *args):
                        mutex.release()
                def hold():
                    with mutex:
                        held.set()
                        if not release.wait(5):
                            expired.set()
                holder = threading.Thread(target=hold)
                holder.start()
                self.assertTrue(held.wait(3))
                async def exercise():
                    cancellation = asyncio.create_task(callback())
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        # This coroutine must resume while publication still
                        # owns the mutex. A blocking async endpoint cannot do so.
                        self.assertFalse(expired.is_set())
                        self.assertFalse(state['cancel'])
                    finally:
                        release.set()
                        await cancellation
                try:
                    with patch.dict(editor.process_state, {task: state}), \
                            patch.object(editor, '_gpu_lock', ObservedMutex()):
                        asyncio.run(exercise())
                finally:
                    release.set()
                    holder.join(6)
                self.assertFalse(holder.is_alive())
                self.assertFalse(expired.is_set())
                self.assertTrue(state['cancel'])
