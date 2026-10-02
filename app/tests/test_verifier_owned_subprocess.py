"""Native CPU verifier ownership of workers which escape their original group."""
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import verify_release as verifier
from tests.test_subprocess_finally import is_live


@unittest.skipUnless(sys.platform == 'linux', 'Native Linux detached ownership')
class VerifierOwnedSubprocessTests(unittest.TestCase):
    def check_tree(self, return_code, interrupt=False, ignore_term=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path, ready, done = [root / name for name in ('pid', 'ready', 'done')]
            leaf = "import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(.2 if sys.argv[3]=='0' else 30);pathlib.Path(sys.argv[2]).touch()"
            if ignore_term:
                leaf = "import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);" + leaf
            worker = """import pathlib,subprocess,sys,time
leaf,pid,ready,done,result=sys.argv[1:]
child=subprocess.Popen([sys.executable,'-c',leaf,ready,done,result],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path(pid).write_text(str(child.pid))
while not pathlib.Path(ready).exists():time.sleep(.005)
print('fixture-ready',flush=True)
if result=='interrupt':time.sleep(30)
else:sys.exit(int(result))
"""
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
            real_print = print
            def interrupting_print(*args, **kwargs):
                if args and 'fixture-ready' in str(args[0]):
                    raise KeyboardInterrupt('fixture verifier interruption')
                real_print(*args, **kwargs)
            try:
                with contextlib.redirect_stdout(io.StringIO()), patch.object(
                        verifier, 'print', side_effect=interrupting_print if interrupt else real_print, create=True):
                    command = [sys.executable, '-c', worker, leaf, str(pid_path), str(ready), str(done),
                               'interrupt' if interrupt else str(return_code)]
                    if interrupt:
                        with self.assertRaisesRegex(KeyboardInterrupt, 'fixture verifier interruption'):
                            verifier.run_command('owned fixture', command, tmp)
                    elif return_code:
                        with self.assertRaisesRegex(RuntimeError, 'exit status 3'):
                            verifier.run_command('owned fixture', command, tmp)
                    else:
                        self.assertIn('fixture-ready', verifier.run_command('owned fixture', command, tmp))
                        self.assertTrue(done.exists(), 'verifier returned before its owned work finished')
                self.assertTrue(pid_path.exists())
                self.assertFalse(is_live(int(pid_path.read_text())), 'escaped worker remained live')
                self.assertIsNone(unrelated.poll(), 'unrelated work was signaled')
            finally:
                if pid_path.exists():
                    pid = int(pid_path.read_text())
                    if is_live(pid):
                        os.kill(pid, signal.SIGKILL)
                unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_failed_worker_stops_detached_descendant(self):
        self.check_tree(3)

    def test_failed_root_force_stops_term_ignoring_detached_child(self):
        from subprocess_ownership import stop_owned_subprocess
        def fast_fixture_stop(process, interrupt=False, timeout=5):
            return stop_owned_subprocess(process, interrupt=interrupt, timeout=.1)
        with patch.object(verifier, 'stop_process_group', side_effect=fast_fixture_stop):
            self.check_tree(3, ignore_term=True)

    def test_success_waits_for_closed_pipe_detached_work(self):
        self.check_tree(0)

    def test_output_exception_stops_detached_descendant(self):
        self.check_tree(3, interrupt=True)


class OwnedNoticeContractTests(unittest.TestCase):
    def test_notice_rejects_malformed_status_without_inventing_success(self):
        import subprocess_ownership as ownership
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'exit.json'
            self.assertIsNone(ownership.get_owned_exit_result(path))
            for value in ('{}', '{"exit_code":true}', '{"exit_code":"0"}', '[]', '{'):
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ownership.get_owned_exit_result(path)
            for code in (0, 3, -2, 0x80000005):
                ownership.save_owned_exit_notice(path, code)
                self.assertEqual(code, ownership.get_owned_exit_result(path))

    def test_windows_factory_forwards_notice_and_existing_verifier_grace(self):
        import subprocess_ownership as ownership
        import windows_subprocess_owner as windows
        command = ['worker', 'argument']
        with patch.object(ownership.sys, 'platform', 'win32'), patch.object(
                windows, 'start_windows_owned_subprocess', return_value='fixture') as spawn:
            self.assertEqual('fixture', ownership.start_owned_subprocess(
                command, exit_notice_path='exit.json', termination_grace=5, cwd='fixture-cwd'))
        spawn.assert_called_once_with(command, exit_notice_path='exit.json',
                                      termination_grace=5, cwd='fixture-cwd')
        self.assertEqual(['worker', 'argument'], command)

    def test_windows_control_uses_caller_grace_and_membership_after_parent_exit(self):
        from types import SimpleNamespace
        import subprocess_ownership as ownership
        import windows_subprocess_owner as windows
        job = SimpleNamespace(handle=123, get_active_process_count=lambda: 2,
                              request_termination=lambda pid, grace: requests.append((pid, grace)))
        requests = []
        process = SimpleNamespace(pid=9001, poll=lambda: 3)
        control = windows.WindowsOwnerControl(job, process, None, termination_grace=5)
        process._alexandria_windows_owner = control
        self.assertTrue(ownership.is_subprocess_tree_running(process))
        control.sendall(f'{int(signal.SIGTERM)}\n'.encode())
        self.assertEqual([(9001, 5)], requests)
        job.handle = None
        self.assertFalse(ownership.is_subprocess_tree_running(process))
