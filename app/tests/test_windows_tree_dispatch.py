"""Windows dispatch contract with disposable native CPU-tree stand-ins."""
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
    except FileNotFoundError:return False


class WindowsTreeDispatchTests(unittest.TestCase):
    def test_owned_windows_job_uses_existing_grace_and_force_dispatch(self):
        from types import SimpleNamespace
        calls = []
        control = SimpleNamespace(
            sendall=lambda data: calls.append(('term', data)),
            terminate=lambda: calls.append(('force',)))
        process = SimpleNamespace(pid=123, _alexandria_control=control,
                                  _alexandria_windows_owner=control)
        with patch.object(core.sys, 'platform', 'win32'), \
             patch.object(core.time, 'monotonic', side_effect=[1, 10.9, 11, 12]), \
             patch.object(core.subprocess, 'run') as taskkill:
            stamp, killed = core.apply_cancel_escalation(process, None, False)
            stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
            self.assertFalse(killed)
            self.assertEqual([('term', b'15\n')], calls)
            stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
            core.apply_cancel_escalation(process, stamp, killed)
            self.assertTrue(killed)
            self.assertEqual([('term', b'15\n'), ('force',)], calls)
            taskkill.assert_not_called()

    @unittest.skipUnless(sys.platform=='linux','Native CPU tree stand-in uses Linux groups')
    def test_grace_and_force_dispatch_to_entire_owned_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            leaf="import pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text('ready');time.sleep(30)"
            code="import pathlib,signal,subprocess,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);pathlib.Path(sys.argv[3]).write_text(str(child.pid));time.sleep(30)"
            process=subprocess.Popen([sys.executable,'-c',code,leaf,str(root/'ready'),str(root/'child.pid')],start_new_session=True)
            unrelated=subprocess.Popen(['sleep','30'],start_new_session=True);child=None;calls=[]
            def taskkill(argv,**kwargs):
                calls.append((argv,kwargs))
                self.assertEqual(['taskkill','/PID',str(process.pid),'/T'],argv[:4])
                os.killpg(process.pid,signal.SIGKILL if '/F' in argv else signal.SIGTERM)
                return subprocess.CompletedProcess(argv,0,'','')
            try:
                deadline=time.monotonic()+3
                while not (root/'ready').exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue((root/'ready').exists());child=int((root/'child.pid').read_text())
                with patch.object(core.sys,'platform','win32'),patch.object(core.subprocess,'run',side_effect=taskkill), \
                     patch.object(core.time,'monotonic',side_effect=[10.,19.9,20.,21.]):
                    stamp,killed=core.apply_cancel_escalation(process,None,False)
                    stamp,killed=core.apply_cancel_escalation(process,stamp,killed)
                    self.assertFalse(killed);self.assertTrue(is_live(child))
                    stamp,killed=core.apply_cancel_escalation(process,stamp,killed)
                    core.apply_cancel_escalation(process,stamp,killed)
                process.wait(timeout=3)
                deadline=time.monotonic()+2
                while is_live(child) and time.monotonic()<deadline:time.sleep(.01)
                self.assertFalse(is_live(child));self.assertTrue(killed);self.assertEqual(2,len(calls))
                self.assertNotIn('/F',calls[0][0]);self.assertIn('/F',calls[1][0]);self.assertIsNone(unrelated.poll())
            finally:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=3);unrelated.terminate();unrelated.wait(timeout=3)

    def test_failed_taskkill_has_one_failure_policy_and_no_parent_only_fallback(self):
        from types import SimpleNamespace
        process=SimpleNamespace(pid=123,kill=lambda:self.fail('parent-only fallback'))
        for error in (subprocess.CalledProcessError(5,['taskkill'],stderr='denied'),
                      subprocess.TimeoutExpired(['taskkill'],10),FileNotFoundError('taskkill unavailable')):
            for forced in (False,True):
                with self.subTest(forced=forced,error=type(error).__name__),patch.object(core.subprocess,'run',side_effect=error):
                    with self.assertRaisesRegex(OSError,'taskkill.*failed'):
                        core.terminate_windows_process_tree(process,force=forced)
