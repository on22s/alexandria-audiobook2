"""Signal only the restart wrapper while a real CPU tree owns a temporary lease."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from tests import test_preparer_restart_lock as restart_fixtures


def is_live(pid):
    try:
        state=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]
        return state not in ('Z','X')
    except FileNotFoundError:return False


def prepare_cpu_tree(root):
    env=restart_fixtures.PreparerRestartLockTests().fixture(root)
    child=root/'app/env/bin/python'
    child.write_text('#!/usr/bin/env python3\n'+r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
descendant=subprocess.Popen([sys.executable,'-c','import time;time.sleep(40)'])
def stop(sig,frame):
 try:descendant.wait(timeout=2)
 except subprocess.TimeoutExpired:descendant.terminate();descendant.wait(timeout=2)
 raise SystemExit(128+sig)
signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)
(root/'pids.json').write_text(json.dumps({'wrapper':os.getppid(),'worker':os.getpid(),
 'descendant':descendant.pid,'group':os.getpgrp(),'argv':sys.argv[1:]}))
while True:time.sleep(.1)
''')
    child.chmod(0o755)
    return env


class RestartSignalTreeTests(unittest.TestCase):
    def test_term_and_hup_to_wrapper_stop_owned_descendants_and_keep_other_process(self):
        for sig,expected in ((signal.SIGTERM,143),(signal.SIGHUP,129)):
            with self.subTest(signal=sig),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);env=prepare_cpu_tree(root)
                env.update(ALEXANDRIA_GPU_LOCK_HELD='1')
                owner='exec 9>"$GPU_LOCK"; flock -x 9; export ALEXANDRIA_GPU_LOCK_PID=$$; "$@" 9>&-; rc=$?; exit "$rc"'
                process=subprocess.Popen(['bash','-c',owner,'CPU-owner','bash',str(root/'run_with_restart.sh')],env=env,cwd=root,
                                         start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                unrelated=subprocess.Popen(['sleep','40'],start_new_session=True)
                pids=None
                try:
                    deadline=time.monotonic()+4
                    while time.monotonic()<deadline:
                        if (root/'pids.json').exists():
                            pids=json.loads((root/'pids.json').read_text());break
                        time.sleep(.01)
                    self.assertIsNotNone(pids);self.assertTrue(is_live(pids['worker']));self.assertTrue(is_live(pids['descendant']))
                    os.kill(pids['wrapper'],sig);process.wait(timeout=5)
                    self.assertEqual(expected,process.returncode)
                    self.assertFalse(is_live(pids['worker']),'preparer survived wrapper shutdown')
                    self.assertFalse(is_live(pids['descendant']),'preparer descendant survived wrapper shutdown')
                    self.assertIsNone(unrelated.poll())
                finally:
                    if pids is not None:
                        for pid in (pids['worker'],pids['descendant']):
                            if is_live(pid):
                                try:os.kill(pid,signal.SIGKILL)
                                except ProcessLookupError:pass
                    if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
                    process.communicate(timeout=3)
                    unrelated.terminate();unrelated.wait(timeout=3)

    def test_pid_only_int_restarts_once_then_double_int_aborts_and_cleans_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=prepare_cpu_tree(root);env.update(ALEXANDRIA_GPU_LOCK_HELD='1')
            owner='exec 9>"$GPU_LOCK"; flock -x 9; export ALEXANDRIA_GPU_LOCK_PID=$$; "$@" 9>&-; rc=$?; exit "$rc"'
            process=subprocess.Popen(['bash','-c',owner,'CPU-owner','bash',str(root/'run_with_restart.sh')],env=env,cwd=root,
                                     start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            rows=[]
            try:
                for attempt in range(2):
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        try:row=json.loads((root/'pids.json').read_text())
                        except (FileNotFoundError,json.JSONDecodeError):time.sleep(.01);continue
                        if not rows or row['worker']!=rows[-1]['worker']:break
                        time.sleep(.01)
                    else:self.fail('preparer did not start/restart')
                    rows.append(row)
                    if attempt:
                        self.assertFalse(is_live(rows[0]['worker']));self.assertFalse(is_live(rows[0]['descendant']))
                        self.assertEqual(1,row['argv'].count('--resume'))
                    os.kill(row['wrapper'],signal.SIGINT)
                process.wait(timeout=5);self.assertEqual(130,process.returncode)
                for row in rows:
                    self.assertFalse(is_live(row['worker']));self.assertFalse(is_live(row['descendant']))
            finally:
                for row in rows:
                    for pid in (row['worker'],row['descendant']):
                        if is_live(pid):
                            try:os.kill(pid,signal.SIGKILL)
                            except ProcessLookupError:pass
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
                process.communicate(timeout=3)

    def test_term_ignoring_worker_is_killed_after_grace_before_owner_exits(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=prepare_cpu_tree(root);env.update(ALEXANDRIA_GPU_LOCK_HELD='1')
            child=root/'app/env/bin/python';child.write_text(child.read_text().replace(
                'signal.signal(signal.SIGTERM,stop)','signal.signal(signal.SIGTERM,signal.SIG_IGN)'))
            owner='exec 9>"$GPU_LOCK"; flock -x 9; export ALEXANDRIA_GPU_LOCK_PID=$$; "$@" 9>&-; rc=$?; exit "$rc"'
            process=subprocess.Popen(['bash','-c',owner,'CPU-owner','bash',str(root/'run_with_restart.sh')],env=env,cwd=root,
                                     start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            row=None
            try:
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    try:row=json.loads((root/'pids.json').read_text());break
                    except (FileNotFoundError,json.JSONDecodeError):time.sleep(.01)
                self.assertIsNotNone(row);started=time.monotonic();os.kill(row['wrapper'],signal.SIGTERM)
                with self.assertRaises(subprocess.TimeoutExpired):process.wait(timeout=.2)
                self.assertTrue(is_live(row['worker']))
                os.kill(row['wrapper'],signal.SIGHUP);os.kill(row['wrapper'],signal.SIGINT)
                self.assertIsNone(process.poll(),'another signal interrupted required cleanup')
                with open(env['GPU_LOCK'],'a') as lock,self.assertRaises(BlockingIOError):
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                process.wait(timeout=25);self.assertGreaterEqual(time.monotonic()-started,19)
                self.assertEqual(143,process.returncode);self.assertFalse(is_live(row['worker']))
                self.assertFalse(is_live(row['descendant']))
                with open(env['GPU_LOCK'],'a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                if row is not None:
                    for pid in (row['worker'],row['descendant']):
                        if is_live(pid):
                            try:os.kill(pid,signal.SIGKILL)
                            except ProcessLookupError:pass
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
                process.communicate(timeout=3)
