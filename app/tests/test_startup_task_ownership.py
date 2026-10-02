"""Actual core claims and lifespan preserve native active chunk/history artifacts."""
import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app as application
import core
from routers import voices, lora
from project import ProjectManager
from run_history import get_run, start_run
from task_ownership import acquire_task_lease, ensure_startup_recovery, TaskOwnershipBusy

APP = Path(__file__).resolve().parents[1]
OWNER = '''import json, pathlib, sys, time
import core
root=pathlib.Path(sys.argv[1])
core.DATA_DIR=str(root)
core.acquire_gpu_lock=lambda:None
core.llm_is_on_this_gpu=lambda:sys.argv[3]=='local'
token=core.reserve_background_task(sys.argv[2])
if sys.argv[4]=='started':core._ensure_owned_task_started(sys.argv[2],token)
(root/'owner-ready').write_text(token)
while not (root/'release-owner').exists():time.sleep(.01)
core.release_gpu_task_claim(sys.argv[2],token)
'''


class StartupTaskOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / 'run_history'
        self.manager = ProjectManager(str(self.root))
        self.manager.save_chunks([{'uid':'native-chunk', 'status':'generating', 'text':'live'},
                                  {'uid':'done-chunk', 'status':'done', 'text':'complete'}])
        self.run_id = start_run(str(self.history), 'audio')
        state = copy.deepcopy(core.process_state)
        for value in state.values():
            value['running'] = False
        self.claims = {}
        for item in (patch.object(core,'DATA_DIR',str(self.root)),
                     patch.object(core,'process_state',state), patch.object(core,'_task_claims',self.claims),
                     patch.object(core,'_gpu_leases',{}), patch.object(core,'acquire_gpu_lock',return_value=None),
                     patch.object(core,'llm_is_on_this_gpu',return_value=True)):
            item.start()
            self.addCleanup(item.stop)
        def close_claims():
            for name, claim in list(self.claims.items()):
                core.release_gpu_task_claim(name,claim['id'])
        self.addCleanup(close_claims)

    def owner(self, task='audio', mode='local', phase='started'):
        process = subprocess.Popen([sys.executable,'-c',OWNER,str(self.root),task,mode,phase],
            env=dict(os.environ,PYTHONPATH=str(APP)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic()+5
        while not (self.root/'owner-ready').is_file() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        if not (self.root/'owner-ready').is_file():
            self.fail('native core claim did not start: '+str(process.communicate(timeout=5)))
        return process

    def startup(self):
        async def start():
            async with application.lifespan(application.app):
                pass
        with patch.object(application,'DATA_DIR',str(self.root)), \
             patch.object(application,'RUN_HISTORY_DIR',str(self.history)), \
             patch.object(application,'EVALUATION_REVIEWS_DIR',str(self.root/'reviews')), \
             patch.object(application,'project_manager',self.manager):
            asyncio.run(start())

    def test_second_server_keeps_live_pending_and_started_chunk_and_history_bytes(self):
        for phase in ('pending','started'):
            with self.subTest(phase=phase):
                process = self.owner(phase=phase)
                paths = [self.root/'chunks.json',self.history/(self.run_id+'.json')]
                before = {path:path.read_bytes() for path in paths}
                self.startup()
                for path, data in before.items():
                    self.assertEqual(data,path.read_bytes())
                (self.root/'release-owner').write_text('done')
                stdout,stderr=process.communicate(timeout=5)
                self.assertEqual(0,process.returncode,stdout+stderr)
                (self.root/'release-owner').unlink()
                (self.root/'owner-ready').unlink()

    def test_dead_server_recovery_resets_only_generating_and_marks_unfinished_history(self):
        process=self.owner()
        process.kill()
        process.communicate(timeout=5)
        self.assertEqual(-signal.SIGKILL,process.returncode)
        self.startup()
        chunks=self.manager.load_chunks()
        self.assertEqual(['pending','done'],[item['status'] for item in chunks])
        record=get_run(str(self.history),self.run_id)
        self.assertEqual('interrupted',record['status'])
        self.assertIsNotNone(record['finished_at'])

    def test_remote_and_cpu_claims_coordinate_across_workers_using_existing_conflicts(self):
        process=self.owner(task='review',mode='remote')
        with patch.object(core,'llm_is_on_this_gpu',return_value=False):
            for name in ('review','persona'):
                with self.subTest(task=name):
                    with self.assertRaises(core.HTTPException) as caught:
                        core.claim_gpu_task(name)
                    self.assertEqual(400,caught.exception.status_code)
                    self.assertFalse(core.process_state[name]['running'])
            token=core.claim_gpu_task('audio')
            core.release_gpu_task_claim('audio',token)
            token=core.claim_gpu_task('m4b_export')
            core.release_gpu_task_claim('m4b_export',token)
        (self.root/'release-owner').write_text('done')
        process.communicate(timeout=5)
        self.assertEqual(0,process.returncode)
        token=core.claim_gpu_task('review')
        core.release_gpu_task_claim('review',token)

    def test_gpu_admission_failure_releases_task_slot_and_stale_token_cannot_release_new_lease(self):
        with patch.object(core,'acquire_gpu_lock',side_effect=OSError('GPU busy')):
            with self.assertRaises(core.HTTPException):
                core.claim_gpu_task('audio')
        lease=acquire_task_lease(self.root,'audio',set())
        lease.close()
        first=core.reserve_background_task('audio')
        core.release_gpu_task_claim('audio',first,pending_only=True)
        second=core.claim_gpu_task('audio')
        self.assertFalse(core.release_gpu_task_claim('audio',first))
        with self.assertRaises(TaskOwnershipBusy):
            acquire_task_lease(self.root,'audio',set())
        core.release_gpu_task_claim('audio',second)
        with ensure_startup_recovery(self.root) as allowed:
            self.assertTrue(allowed)

    def test_cpu_export_claim_is_shared_but_other_cpu_exports_remain_independent(self):
        process=self.owner(task='m4b_export',phase='pending')
        with self.assertRaises(core.HTTPException) as caught:
            core.claim_gpu_task('m4b_export')
        self.assertEqual(400,caught.exception.status_code)
        token=core.claim_gpu_task('audacity_export')
        core.release_gpu_task_claim('audacity_export',token)
        before=(self.root/'chunks.json').read_bytes()
        self.startup()
        self.assertEqual(before,(self.root/'chunks.json').read_bytes())
        (self.root/'release-owner').write_text('done')
        process.communicate(timeout=5)
        self.assertEqual(0,process.returncode)

    def test_pending_shutdown_releases_its_lease_but_started_task_keeps_recovery_closed(self):
        pending=core.reserve_background_task('audacity_export')
        started=core.reserve_background_task('m4b_export')
        self.assertTrue(core._ensure_owned_task_started('m4b_export',started))
        core.release_pending_task_claims()
        self.assertNotIn('audacity_export',core._task_claims)
        self.assertEqual(started,core._task_claims['m4b_export']['id'])
        lease=acquire_task_lease(self.root,'audacity_export',set())
        lease.close()
        with ensure_startup_recovery(self.root) as allowed:
            self.assertFalse(allowed)
        core.release_gpu_task_claim('m4b_export',started)
        with ensure_startup_recovery(self.root) as allowed:
            self.assertTrue(allowed)

    def test_cancelled_suggestion_and_checkpoint_requests_keep_lease_until_thread_exits(self):
        cases=[('voices',voices,'_suggest_voices_impl',lambda:voices.suggest_voices(voices.SuggestVoicesRequest())),
               ('lora_training',lora,'_promote_lora_candidate',lambda:lora.lora_promote_candidate('fixture')),
               ('lora_training',lora,'_rollback_lora_promotion',lambda:lora.lora_rollback_promotion('fixture')),
               ('lora_training',lora,'_recover_checkpoint_swap',lambda:lora.lora_recover_checkpoint_swap('fixture'))]
        for name,module,callback,route in cases:
            with self.subTest(callback=callback):
                arrived,release=threading.Event(),threading.Event()
                def work(*args, **kwargs):
                    arrived.set()
                    if not release.wait(5):raise AssertionError('worker not released')
                    return {'candidate':'CPU fixture'}
                async def exercise():
                    request=asyncio.create_task(route())
                    try:
                        self.assertTrue(await asyncio.to_thread(arrived.wait,2))
                        request.cancel()
                        with self.assertRaises(asyncio.CancelledError):await request
                        with self.assertRaises(TaskOwnershipBusy):acquire_task_lease(self.root,name,set())
                        with ensure_startup_recovery(self.root) as allowed:self.assertFalse(allowed)
                    finally:
                        release.set()
                        for _ in range(200):
                            if name not in core._task_claims:break
                            await asyncio.sleep(.01)
                    self.assertNotIn(name,core._task_claims)
                    lease=acquire_task_lease(self.root,name,set());lease.close()
                with patch.object(module,callback,side_effect=work):
                    asyncio.run(exercise())
