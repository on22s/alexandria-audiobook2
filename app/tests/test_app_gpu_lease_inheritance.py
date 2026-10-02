"""App-owned lease proof across native child processes without inheriting its FD."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

import core
from tests import test_gpu_owner_death
from tests.test_subprocess_finally import is_live

ROOT = Path(__file__).resolve().parents[2]


class AppGpuLeaseInheritanceTest(unittest.TestCase):
    def test_explicit_descriptor_requires_a_held_matching_ancestor_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'gpu.lock'
            with lock.open('a') as handle, (Path(tmp) / 'other.lock').open('a') as other:
                fd = str(handle.fileno())
                environment = dict(os.environ, GPU_LOCK=str(lock))
                command = ['bash', str(ROOT/'gpu_job.sh'), '--check-lock-owner', str(os.getpid())]
                def probe(descriptor):
                    return subprocess.run(command+[descriptor], env=environment,
                                          close_fds=True, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(0, probe(fd).returncode)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(0, probe(fd).returncode)
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for bad in (str(other.fileno()), '', '0', '-1', 'not-fd', '../../9'):
                    with self.subTest(descriptor=bad):
                        result = probe(bad)
                        self.assertNotEqual(0, result.returncode, result.stderr)

    def test_app_owned_stream_and_nested_enricher_reuse_lease_through_model_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock, events, source, output = [root/name for name in ('gpu.lock','events.jsonl','input.json','output.json')]
            chunk = {'text':'hello','start':0,'end':1}
            source.write_text(json.dumps([chunk]))
            (root/'llama_cpp.py').write_text('''import fcntl,json,os,pathlib
def record(event):
    with open(os.environ['GPU_LOCK'],'a') as probe:
        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise AssertionError('GPU lease absent during '+event)
    with open(os.environ['EVENTS'],'a') as f:f.write(json.dumps(event)+'\\n')
def llama_supports_gpu_offload():return True
class Llama:
    def __init__(self,**kwargs):record('load')
    def __call__(self,*args,**kwargs):
        record('infer')
        return {'choices':[{'text':'{"emotional_tone":"calm"}'}]}
    def close(self):record('close')
''')
            state = {'logs':[], 'running':True, 'cancel':False, 'process':None, 'pid':None}
            environment = dict(os.environ, PYTHONPATH=str(root)+os.pathsep+str(ROOT/'app'),
                               EVENTS=str(events), GPU_LOCK=str(lock),
                               ALEXANDRIA_GPU_LOCK_HELD='0')
            before = dict(environment)
            command = [sys.executable, '-c', 'import subprocess,sys;raise SystemExit(subprocess.run(sys.argv[1:],close_fds=True).returncode)',
                       sys.executable, str(ROOT/'llm_enricher.py'), '--model-path','fixture.gguf',
                       '--input-file',str(source),'--output-file',str(output),'--emotional-tone']
            with lock.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(core, 'process_state', {'preparer':state}), \
                     patch.object(core, '_gpu_leases', {'preparer':handle}), \
                     patch.object(core, '_task_claims', {}):
                    code, lines = core._stream_subprocess_to_logs(command, str(root), state, env=environment)
                self.assertEqual(0, code, '\n'.join(lines))
                self.assertEqual(before, environment)
                self.assertFalse(handle.closed, 'child cannot release app owner handle')
                with lock.open('a') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(['load','infer','close'],[json.loads(line) for line in events.read_text().splitlines()])
            self.assertEqual([{**chunk,'emotional_tone':'calm'}],json.loads(output.read_text()))
            with lock.open('a') as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_environment_does_not_borrow_another_tasks_lease(self):
        with tempfile.TemporaryDirectory() as tmp, (Path(tmp)/'gpu.lock').open('a') as handle:
            owned, other = {'running':True}, {'running':True}
            original = {'KEEP':'value'}
            with patch.object(core, 'process_state', {'preparer':owned}), \
                 patch.object(core, '_gpu_leases', {'preparer':handle}), \
                 patch.object(core, '_task_claims', {}):
                alternate = dict(original, GPU_LOCK=str(Path(tmp)/'incorrect.lock'))
                self.assertEqual(str(Path(tmp)/'gpu.lock'), core.get_gpu_task_environment(owned, alternate)['GPU_LOCK'])
                self.assertEqual(original, core.get_gpu_task_environment(other, original))
                owned['running'] = False
                self.assertEqual(original, core.get_gpu_task_environment(owned, original))
            self.assertEqual({'KEEP':'value'}, original)


class RetainedAppLeaseDeathTest(unittest.TestCase):
    setUp = test_gpu_owner_death.GpuOwnerDeathTests.setUp

    def test_killed_app_keeps_lease_while_owner_is_paused_and_workers_are_live(self):
        lock = self.root / 'gpu.lock'
        pids_path = self.root / 'pids.json'
        owner_path = self.root / 'owner.pid'
        worker = "import json,os,pathlib,subprocess,sys;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]));child.wait()"
        launcher = r"""
import core,fcntl,os,pathlib,sys
lock,owner_path,pids_path,worker=sys.argv[1:]
handle=open(lock,'a');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
state={'logs':[],'running':True,'cancel':False,'process':None,'pid':None}
core.process_state={'preparer':state};core._gpu_leases={'preparer':handle};core._task_claims={}
original=core.start_owned_subprocess
def start(*args,**kwargs):
    process=original(*args,**kwargs)
    pathlib.Path(owner_path).write_text(str(process.pid))
    return process
core.start_owned_subprocess=start
core._stream_subprocess_to_logs([sys.executable,'-c',worker,pids_path],str(pathlib.Path(lock).parent),state)
"""
        environment = dict(os.environ, PYTHONPATH=str(ROOT/'app'),
                           GPU_LOCK=str(lock), ALEXANDRIA_GPU_LOCK_HELD='0')
        parent = subprocess.Popen([sys.executable,'-c',launcher,str(lock),str(owner_path),str(pids_path),worker],
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sentinel = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        owner = None
        pids = []
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline and not (owner_path.exists() and pids_path.exists()):
                time.sleep(.01)
            self.assertTrue(owner_path.exists() and pids_path.exists(), 'owned worker must be admitted')
            owner = int(owner_path.read_text())
            pids = json.loads(pids_path.read_text())
            os.kill(owner, signal.SIGSTOP)
            parent.kill()
            parent.wait(timeout=5)
            self.assertTrue(all(is_live(pid) for pid in pids))
            with lock.open('a') as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
            os.kill(owner, signal.SIGCONT)
            deadline = time.monotonic()+5
            free = False
            with lock.open('a') as probe:
                while time.monotonic()<deadline:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
                        free = True
                        fcntl.flock(probe, fcntl.LOCK_UN)
                    except BlockingIOError:
                        free = False
                    live = any(is_live(pid) for pid in pids)
                    self.assertFalse(free and live, 'lease released before owned workers stopped')
                    if free and not live:
                        break
                    time.sleep(.01)
            self.assertTrue(free)
            self.assertFalse(any(is_live(pid) for pid in pids))
            self.assertIsNone(sentinel.poll())
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if owner is not None:
                try:
                    os.kill(owner, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            for pid in pids:
                if is_live(pid):
                    os.kill(pid, signal.SIGKILL)
            if owner is not None:
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    try:
                        reaped,_=os.waitpid(owner,os.WNOHANG)
                    except ChildProcessError:
                        break
                    if reaped:
                        break
                    time.sleep(.01)
            sentinel.kill()
            sentinel.wait(timeout=5)
