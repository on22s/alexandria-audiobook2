"""CPU claims retain pending ownership without occupying the global GPU slot."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from fastapi import BackgroundTasks, HTTPException
import core


class CpuTaskClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.states=copy.deepcopy(core.process_state)
        for state in self.states.values():state['running']=False
        self.contexts=[patch.object(core,'DATA_DIR',self.tmp.name),patch.object(core,'process_state',self.states),
                      patch.object(core,'_task_claims',{}),patch.object(core,'_gpu_leases',{}),
                      patch.object(core,'llm_is_on_this_gpu',return_value=True),
                      patch.object(core,'acquire_gpu_lock',return_value=None)]
        for ctx in self.contexts:ctx.start();self.addCleanup(ctx.stop)
        self.addCleanup(lambda:[core.release_gpu_task_claim(name, owner['id']) for name,owner in list(core._task_claims.items())])

    def test_cpu_and_gpu_admission_both_orders_preserve_double_start_exclusion(self):
        for first in ('cpu','gpu'):
            with self.subTest(first=first):
                cpu = lambda:core.claim_gpu_task('voicelab',cpu_only=True)
                gpu = lambda:core.claim_gpu_task('audio')
                one=cpu() if first=='cpu' else gpu()
                two=gpu() if first=='cpu' else cpu()
                self.assertTrue(core.is_task_running('voicelab'))
                self.assertTrue(core.is_task_running('audio'))
                self.assertTrue(core._task_claims['voicelab']['cpu_only'])
                with self.assertRaises(HTTPException):core.claim_gpu_task('voicelab',cpu_only=True)
                with self.assertRaises(HTTPException):core.claim_gpu_task('voicelab')
                core.release_gpu_task_claim('voicelab');core.release_gpu_task_claim('audio')
        self.assertEqual(2,core.acquire_gpu_lock.call_count)

    def test_pending_cpu_claim_is_retained_until_callback_and_registration_errors_release(self):
        tasks=BackgroundTasks();calls=[]
        token=core.schedule_claimed_background_task(tasks,'voicelab',lambda value:calls.append(value),'fixture',cpu_only=True)
        self.assertEqual('pending',core._task_claims['voicelab']['phase'])
        self.assertTrue(Path(self.tmp.name,'.task_ownership/task-cpu__voicelab.lock').is_file())
        core.acquire_gpu_lock.assert_not_called()
        audio=core.claim_gpu_task('audio');core.release_gpu_task_claim('audio',audio)
        asyncio.run(tasks())
        self.assertEqual(['fixture'],calls)
        self.assertFalse(core.is_task_running('voicelab'))
        self.assertNotIn('voicelab',core._task_claims)
        class RejectTasks(BackgroundTasks):
            def add_task(self,*args,**kwargs):raise RuntimeError('registration failed')
        with self.assertRaisesRegex(RuntimeError,'registration failed'):
            core.schedule_claimed_background_task(RejectTasks(),'voicelab',Mock(),cpu_only=True)
        self.assertFalse(core.is_task_running('voicelab'))
        self.assertNotIn('voicelab',core._task_claims)

    def test_gpu_voicelab_and_unknown_legacy_state_remain_conflicting(self):
        owner=core.claim_gpu_task('voicelab')
        with self.assertRaises(HTTPException):core.claim_gpu_task('audio')
        core.release_gpu_task_claim('voicelab',owner)
        self.states['voicelab']['running']=True
        self.states['voicelab']['cpu_only']=True # Unowned/stale metadata cannot bypass admission.
        with self.assertRaises(HTTPException):core.claim_gpu_task('audio')
        self.states['voicelab']['running']=False

    def test_cancelled_request_keeps_started_cpu_owner_until_worker_exits(self):
        tasks=BackgroundTasks();arrived=threading.Event();release=threading.Event();finished=threading.Event()
        def work():
            self.states['voicelab']['running']=False
            arrived.set()
            try:
                if not release.wait(5):raise AssertionError('worker not released')
            finally:finished.set()
        async def application(scope,receive,send):
            core.schedule_claimed_background_task(tasks,'voicelab',work,cpu_only=True)
            await tasks()
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):pass
        async def exercise():
            request=asyncio.create_task(core.TaskClaimMiddleware(application)({'type':'http'},receive,send))
            try:
                self.assertTrue(await asyncio.to_thread(arrived.wait,2))
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):await request
                self.assertTrue(core.is_task_running('voicelab'))
                with self.assertRaises(HTTPException):core.claim_gpu_task('voicelab')
                audio=core.claim_gpu_task('audio');core.release_gpu_task_claim('audio',audio)
            finally:
                release.set();self.assertTrue(await asyncio.to_thread(finished.wait,2))
                for _ in range(100):
                    if not core.is_task_running('voicelab'):break
                    await asyncio.sleep(.01)
            self.assertFalse(core.is_task_running('voicelab'))
        asyncio.run(exercise())
