"""Server death retains the task lease until the native Linux owner reaps workers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

from task_ownership import acquire_task_lease, ensure_startup_recovery, TaskOwnershipBusy
from tests import test_gpu_owner_death
from tests.test_subprocess_finally import is_live

APP = Path(__file__).resolve().parents[1]


class RetainedTaskLeaseTests(unittest.TestCase):
    setUp = test_gpu_owner_death.GpuOwnerDeathTests.setUp

    def test_killed_server_lease_survives_in_paused_owner_until_all_workers_are_reaped(self):
        self.check_server_lease()

    def test_killed_server_cpu_lease_survives_until_all_workers_are_reaped(self):
        self.check_server_lease(cpu_only=True)

    def check_server_lease(self, cpu_only=False):
        owner_path, pids_path = self.root/'owner.pid', self.root/'workers.json'
        worker = '''import json,os,pathlib,subprocess,sys
try:os.fstat(int(sys.argv[2]))
except OSError:pass
else:raise AssertionError('worker inherited task ownership descriptor')
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
child.wait()
'''
        launcher = '''import pathlib,sys
import core
root,worker,mode=sys.argv[1:]
core.DATA_DIR=root
core.acquire_gpu_lock=lambda:None
token=core.reserve_background_task('preparer',cpu_only=mode=='cpu')
core._ensure_owned_task_started('preparer',token)
state=core.process_state['preparer']
original=core.start_owned_subprocess
def start(*args,**kwargs):
    process=original(*args,**kwargs)
    (pathlib.Path(root)/'owner.pid').write_text(str(process.pid))
    return process
core.start_owned_subprocess=start
descriptor=core.get_task_lease_descriptor(state)
core._stream_subprocess_to_logs([sys.executable,'-c',worker,str(pathlib.Path(root)/'workers.json'),str(descriptor)],root,state)
core.release_gpu_task_claim('preparer',token)
'''
        parent = subprocess.Popen([sys.executable,'-c',launcher,str(self.root),worker,'cpu' if cpu_only else 'gpu'],
            env=dict(os.environ,PYTHONPATH=str(APP)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        sentinel = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        owner, pids = None, []
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and parent.poll() is None and not (owner_path.exists() and pids_path.exists()):
                time.sleep(.01)
            self.assertTrue(owner_path.exists() and pids_path.exists(),'actual worker tree not admitted')
            owner=int(owner_path.read_text())
            pids=json.loads(pids_path.read_text())
            self.assertTrue(all(is_live(pid) for pid in pids))
            os.kill(owner,signal.SIGSTOP)
            parent.kill()
            parent.communicate(timeout=5)
            self.assertTrue(all(is_live(pid) for pid in pids))
            with self.assertRaises(TaskOwnershipBusy):
                acquire_task_lease(self.root,'preparer',set())
            with ensure_startup_recovery(self.root) as allowed:
                self.assertFalse(allowed)
            os.kill(owner,signal.SIGCONT)
            deadline=time.monotonic()+5
            free=False
            while time.monotonic()<deadline:
                try:
                    lease=acquire_task_lease(self.root,'preparer',set())
                except TaskOwnershipBusy:
                    free=False
                else:
                    lease.close()
                    free=True
                live=any(is_live(pid) for pid in pids)
                self.assertFalse(free and live,'task admission preceded owned worker reaping')
                if free and not live:
                    break
                time.sleep(.01)
            self.assertTrue(free)
            self.assertFalse(any(is_live(pid) for pid in pids))
            self.assertIsNone(sentinel.poll(),'unrelated process was signalled')
            with ensure_startup_recovery(self.root) as allowed:
                self.assertTrue(allowed)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)
            if owner is not None:
                try:os.kill(owner,signal.SIGCONT)
                except ProcessLookupError:pass
            for pid in pids:
                if is_live(pid):
                    os.kill(pid,signal.SIGKILL)
            if owner is not None:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:reaped,_=os.waitpid(owner,os.WNOHANG)
                    except ChildProcessError:break
                    if reaped:break
                    time.sleep(.01)
            sentinel.kill()
            sentinel.wait(timeout=5)
