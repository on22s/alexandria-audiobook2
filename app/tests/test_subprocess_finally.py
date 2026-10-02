"""Real subprocesses and descriptor ownership on exceptional stream exits."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import core


def is_live(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0] not in ('Z','X')
    except (FileNotFoundError, ProcessLookupError):return False


class StreamFinallyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Native Linux process-group artifact checks")
    def test_keyboard_interrupt_removes_live_handle_and_stops_owned_group(self):
        class FailedLogs(list):
            def extend(self,value):raise KeyboardInterrupt('fixture log interruption')
        with tempfile.TemporaryDirectory() as tmp:
            state={'logs':FailedLogs(),'processes':[],'process':None,'pid':None,'cancel':False}
            handles=[];popen=subprocess.Popen;opened=[];real_open=open
            def spawn(*args,**kwargs):
                process=popen(*args,**kwargs);handles.append(process);return process
            def tracking_open(*args,**kwargs):
                handle=real_open(*args,**kwargs);opened.append(handle);return handle
            code="import subprocess,sys,time,pathlib;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);pathlib.Path(sys.argv[1]).write_text(str(child.pid));print('fault',flush=True);time.sleep(30)"
            try:
                with patch.object(core.subprocess,'Popen',side_effect=spawn),patch.object(core,'open',side_effect=tracking_open,create=True):
                    with self.assertRaisesRegex(KeyboardInterrupt,'fixture log interruption'):
                        core._stream_subprocess_to_logs([sys.executable,'-c',code,str(Path(tmp)/'child.pid')],tmp,state,log_file=str(Path(tmp)/'run.log'))
                self.assertEqual([],state['processes']);self.assertIsNone(state['process']);self.assertIsNone(state['pid'])
                self.assertIsNotNone(handles[0].poll());self.assertTrue(all(handle.closed for handle in opened))
                self.assertFalse(is_live(int((Path(tmp)/'child.pid').read_text())))
            finally:
                for process in handles:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait(timeout=3)
                    if process.stdout is not None:process.stdout.close()

    def test_wait_failure_still_clears_handle_and_closes_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            state={'logs':[],'processes':[],'process':None,'pid':None,'cancel':False}
            handles=[];popen=subprocess.Popen
            def spawn(*args,**kwargs):
                process=popen(*args,**kwargs);handles.append(process);wait=process.wait;count=0
                def fail_once(*args,**kwargs):
                    nonlocal count
                    count+=1
                    if count==1:raise OSError('fixture wait failure')
                    return wait(*args,**kwargs)
                process.wait=fail_once;return process
            try:
                with patch.object(core.subprocess,'Popen',side_effect=spawn),self.assertRaisesRegex(OSError,'fixture wait failure'):
                    core._stream_subprocess_to_logs([sys.executable,'-c','pass'],tmp,state)
                self.assertEqual([],state['processes']);self.assertIsNone(state['process']);self.assertIsNone(state['pid'])
                self.assertTrue(handles[0].stdout.closed)
            finally:
                for process in handles:process.wait(timeout=3)

    def test_launch_failure_closes_already_opened_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            state={'logs':[],'processes':[],'process':None,'pid':None,'cancel':False}
            real_open=open;opened=[]
            def tracking_open(*args,**kwargs):
                handle=real_open(*args,**kwargs);opened.append(handle);return handle
            with patch.object(core,'open',side_effect=tracking_open,create=True),self.assertRaises(FileNotFoundError):
                core._stream_subprocess_to_logs([str(Path(tmp)/'missing-program')],tmp,state,log_file=str(Path(tmp)/'run.log'))
            self.assertEqual([],state['processes']);self.assertTrue(opened);self.assertTrue(all(handle.closed for handle in opened))

    @unittest.skipUnless(sys.platform == "linux", "Native Linux process-group artifact checks")
    def test_task_returns_only_after_the_wrappers_owned_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            state={'logs':[],'processes':[],'process':None,'pid':None,'cancel':False}
            handles=[];popen=subprocess.Popen;pid=None
            def spawn(*args,**kwargs):
                process=popen(*args,**kwargs);handles.append(process);return process
            code="import pathlib,subprocess,sys;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.15)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            try:
                with patch.object(core.subprocess,'Popen',side_effect=spawn):
                    rc,lines=core._stream_subprocess_to_logs([sys.executable,'-c',code,str(Path(tmp)/'child.pid')],tmp,state)
                self.assertEqual(0,rc);self.assertEqual([],state['processes'])
                pid=int((Path(tmp)/'child.pid').read_text());self.assertFalse(is_live(pid))
                self.assertEqual(0,handles[0].poll())
                self.assertTrue(handles[0].stdout.closed)
            finally:
                if pid is not None and is_live(pid):os.kill(pid,signal.SIGKILL)
