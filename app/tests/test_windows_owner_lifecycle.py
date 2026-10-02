"""Windows owner admission/error paths plus a native CPU lifetime stand-in."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import windows_subprocess_owner as owner
from tests.test_subprocess_finally import is_live


class AdmissionJob:
    def __init__(self, fail=False):
        self.handle = 123
        self.fail = fail
        self.calls = []
        self.active = 0

    def apply_process_membership(self, process_handle):
        self.calls.append('assign')
        if self.fail:
            raise OSError('assignment denied')
        self.active = 1

    def get_active_process_count(self):
        return self.active

    def terminate(self):
        self.calls.append('terminate')
        self.active = 0

    def ensure_stopped(self):
        if self.active:
            raise AssertionError('released live membership')
        self.calls.append('stopped')

    def close(self):
        self.calls.append('close')
        self.handle = None


class WindowsOwnerAdmissionTests(unittest.TestCase):
    def check_admission(self, fail_assignment=False, fail_worker=False):
        job = AdmissionJob(fail_assignment)
        observed = []
        class Process:
            _handle = 0x200000002
            pid = 9001
            stdin = stdout = stderr = None
            killed = False
            def kill(self):
                self.killed = True
            def wait(self):
                return 0
            def poll(self):
                gate, receipt = observed[0]
                self_case.assertTrue(gate.exists(), 'worker started before admission gate')
                self_case.assertIn('assign', job.calls)
                if not receipt.exists():
                    owner.save_windows_owner_message(receipt,
                        {'error': 'worker executable missing', 'errno': 2} if fail_worker
                        else {'started': 9002})
                return None
        self_case = self
        process = Process()
        def spawn(argv, **kwargs):
            gate, receipt = map(Path, argv[2:4])
            self.assertFalse(gate.exists(), 'gate opened before process membership')
            self.assertEqual([], job.calls)
            observed.append((gate, receipt))
            self.assertEqual(['worker', 'argument'], argv[4:])
            self.assertEqual('fixture-cwd', kwargs['cwd'])
            return process
        with patch.object(owner, 'WindowsSubprocessJob', return_value=job), \
             patch.object(owner.subprocess, 'Popen', side_effect=spawn):
            if fail_assignment or fail_worker:
                with self.assertRaisesRegex(OSError, 'assignment denied|worker executable missing'):
                    owner.start_windows_owned_subprocess(['worker', 'argument'], cwd='fixture-cwd')
                self.assertIsNone(job.handle)
                self.assertFalse(observed[0][0].parent.exists())
                if fail_assignment:
                    self.assertTrue(process.killed)
                    self.assertNotIn('terminate', job.calls)
                else:
                    self.assertIn('stopped', job.calls)
            else:
                result = owner.start_windows_owned_subprocess(['worker', 'argument'], cwd='fixture-cwd')
                self.assertIs(result, process)
                self.assertIs(result._alexandria_windows_owner, result._alexandria_control)
                self.assertIsNotNone(job.handle)
                result._alexandria_control.close()
                self.assertLess(job.calls.index('stopped'), job.calls.index('close'))
                self.assertFalse(observed[0][0].parent.exists())

    def test_worker_admission_occurs_after_kernel_assignment(self):
        self.check_admission()

    def test_failed_kernel_assignment_never_opens_worker_gate(self):
        self.check_admission(fail_assignment=True)

    def test_failed_worker_admission_stops_the_entire_job_before_return(self):
        self.check_admission(fail_worker=True)

    def test_failed_temporary_directory_does_not_leak_job_handle(self):
        job = AdmissionJob()
        with patch.object(owner, 'WindowsSubprocessJob', return_value=job), \
             patch.object(owner.tempfile, 'TemporaryDirectory', side_effect=OSError('temp denied')):
            with self.assertRaisesRegex(OSError, 'temp denied'):
                owner.start_windows_owned_subprocess(['worker'])
        self.assertIsNone(job.handle)

    def test_shutdown_retains_ownership_across_kernel_query_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = AdmissionJob()
            job.active = 1
            reads = [OSError('query temporarily failed'), 1]
            def query():
                if reads:
                    value = reads.pop(0)
                    if isinstance(value, Exception):
                        raise value
                    return value
                return job.active
            directory = SimpleNamespace(cleanup=lambda: job.calls.append('directory-cleaned'))
            control = owner.WindowsOwnerControl(job, SimpleNamespace(pid=9001), directory)
            with patch.object(job, 'get_active_process_count', side_effect=query), \
                 patch.object(owner.logging, 'exception') as log, patch.object(owner.time, 'sleep') as sleep:
                control.close()
            self.assertEqual(1, log.call_count)
            self.assertEqual(1, sleep.call_count)
            self.assertEqual(['terminate', 'stopped', 'close', 'directory-cleaned'], job.calls)

    def test_helper_without_exclusive_membership_never_spawns_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / 'gate'
            gate.touch()
            for count in (0, 2):
                with self.subTest(count=count), patch.object(owner, 'get_windows_job_api'), \
                     patch.object(owner, 'get_windows_job_active_process_count', return_value=count), \
                     patch.object(owner.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(OSError, 'exclusive initial job membership'):
                        owner.run_windows_owned_command(gate, Path(tmp) / 'receipt', ['worker'])
                    spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'CPU stand-in uses Linux process observations')
    def test_native_closed_pipe_detached_child_outlives_wrapper_but_not_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate, receipt, pids = root / 'gate', root / 'receipt', root / 'pids'
            exit_notice = root / 'exit-notice'
            gate.touch()
            worker = """import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
"""
            result = []
            registered = []
            def membership(api, handle=None):
                if pids.exists():
                    registered[:] = json.loads(pids.read_text())
                return 1 + sum(is_live(pid) for pid in registered)
            def run():
                try:
                    result.append(owner.run_windows_owned_command(
                        gate, receipt, [sys.executable, '-c', worker, str(pids)], exit_notice_path=exit_notice))
                except BaseException as error:
                    result.append(error)
            thread = threading.Thread(target=run)
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
            try:
                with patch.object(owner, 'get_windows_job_api', return_value=object()), \
                     patch.object(owner, 'get_windows_job_active_process_count', side_effect=membership):
                    thread.start()
                    deadline = time.monotonic() + 5
                    while not registered and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(2, len(registered))
                    deadline = time.monotonic() + 3
                    while is_live(registered[0]) and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertFalse(is_live(registered[0]), 'wrapper has not exited')
                    self.assertTrue(is_live(registered[1]))
                    deadline = time.monotonic() + 3
                    while not exit_notice.exists() and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertEqual({'exit_code': 0}, json.loads(exit_notice.read_text()))
                    self.assertTrue(thread.is_alive(), 'owner returned over detached child')
                    self.assertEqual([], result)
                    os.kill(registered[1], signal.SIGKILL)
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual([0], result)
                    self.assertIsNone(sentinel.poll())
            finally:
                for pid in registered:
                    if is_live(pid):
                        os.kill(pid, signal.SIGKILL)
                thread.join(timeout=5)
                sentinel.terminate()
                sentinel.wait(timeout=3)
