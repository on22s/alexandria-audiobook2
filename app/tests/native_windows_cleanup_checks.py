"""Explicit native Windows cleanup checks; other hosts refuse before app imports."""
import sys

if sys.platform != 'win32':
    print('Native Windows cleanup checks require Windows; nothing was verified.', file=sys.stderr)
    raise SystemExit(2)

import contextlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import verify_release as verifier
import run_isolated_api_tests as runner
from subprocess_ownership import is_subprocess_tree_running, start_owned_subprocess
from subprocess_ownership import run_owned_capture


def _base_python():
    return getattr(sys, '_base_executable', None) or sys.executable


class WindowsHeadlessCleanupEscalationTests(unittest.TestCase):
    def _cancel_review_tree(self, cooperative, fail_force=False):
        import core
        import review_script as review
        from windows_subprocess_job import WindowsSubprocessJob
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'book.json'
            rows = [{'speaker': 'ANN', 'text': 'Saved.', 'instruct': 'Plain.'}]
            output.write_text(json.dumps(rows), encoding='utf-8')
            review.save_checkpoint(str(output), 1, 2, 1, 0, rows, {}, None,
                                   [1], [], 'source', None)
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            ready = root / 'ready.json'
            child_code = (
                'import os,time,pathlib; '
                'path=pathlib.Path(os.environ.get("ALEXANDRIA_REVIEW_CANCEL_FILE", "missing")); '
                + ('\nwhile not path.exists(): time.sleep(.02)' if cooperative else 'time.sleep(60)'))
            worker = (
                'import ctypes,json,os,pathlib,subprocess,sys,time\n'
                'ctypes.windll.kernel32.FreeConsole()\n'
                + ('from review_script import ensure_review_not_cancelled\n' if cooperative else '')
                +
                f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}],'
                'creationflags=subprocess.DETACHED_PROCESS)\n'
                f'pathlib.Path({str(ready)!r}).write_text(json.dumps('
                '[os.getpid(),child.pid,bool(ctypes.windll.kernel32.GetConsoleWindow())]))\n'
                + ('while True:\n ensure_review_not_cancelled()\n time.sleep(.02)\n'
                   if cooperative else 'time.sleep(60)\n'))
            state = dict(running=False, cancel=False, status='idle', logs=[],
                         process=None, pid=None, processes=[], return_code=None)
            processes, closed_counts, forced = [], [], []
            watchdog = threading.Event()
            real_terminate = WindowsSubprocessJob.terminate
            real_close = WindowsSubprocessJob.close
            def start(*args, **kwargs):
                process = start_owned_subprocess(*args, creationflags=subprocess.DETACHED_PROCESS, **kwargs)
                processes.append(process)
                return process
            def terminate(job, exit_code=1):
                forced.append(time.monotonic())
                if fail_force and len(forced) == 1:
                    raise OSError('injected review force-stop failure')
                return real_terminate(job, exit_code)
            def close(job):
                closed_counts.append(job.get_active_process_count())
                return real_close(job)
            def rescue():
                watchdog.set()
                for process in processes:
                    real_terminate(process._alexandria_windows_owner.job)
            timer = threading.Timer(25, rescue)
            thread = threading.Thread(target=core.run_process,
                args=([sys.executable, '-u', '-c', worker], 'review', os.getcwd()))
            try:
                with patch.dict(core.process_state, {'review': state}), \
                     patch.object(core, '_init_task_log', return_value=str(root / 'review.log')), \
                     patch.object(core, 'start_owned_subprocess', side_effect=start), \
                     patch.object(core, 'CANCEL_TERMINATE_GRACE_SECONDS', .5), \
                     patch.object(WindowsSubprocessJob, 'terminate', new=terminate), \
                     patch.object(WindowsSubprocessJob, 'close', new=close), \
                     self.assertLogs('AlexandriaUI', level='INFO') as captured:
                    timer.start()
                    thread.start()
                    deadline = time.monotonic() + 15
                    while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), state['logs'])
                    worker_pid, child_pid, has_console = json.loads(ready.read_text())
                    self.assertFalse(has_console, 'fixture worker unexpectedly has a console')
                    job = processes[0]._alexandria_windows_owner.job
                    self.assertIn(worker_pid, job.get_process_ids())
                    self.assertIn(child_pid, job.get_process_ids())
                    requested = time.monotonic()
                    core._cancel_task('review', 'idle', 'exited')
                    thread.join(20)
                self.assertFalse(thread.is_alive(), 'cancel did not complete within its bound')
                self.assertFalse(watchdog.is_set(), 'cancel required watchdog cleanup')
                self.assertEqual([0], closed_counts)
                self.assertFalse(is_subprocess_tree_running(processes[0]))
                self.assertIsNotNone(processes[0].poll())
                self.assertFalse(state['running'])
                self.assertIsNone(state['process'])
                self.assertIsNone(state['pid'])
                self.assertEqual([], state['processes'])
                self.assertEqual('failed' if fail_force else 'cancelled', state['status'])
                if fail_force:
                    self.assertNotIn('Task review cancelled.', state['logs'])
                    self.assertTrue(any(record.levelno >= logging.ERROR for record in captured.records))
                    self.assertIn('injected review force-stop failure', '\n'.join(state['logs']))
                else:
                    self.assertFalse(any(record.levelno >= logging.WARNING for record in captured.records), captured.output)
                    self.assertNotIn('ERROR:', '\n'.join(state['logs']))
                    self.assertIn('Task review cancelled.', state['logs'])
                if cooperative:
                    self.assertEqual([], forced, 'cooperative review needed forced termination')
                else:
                    self.assertGreaterEqual(forced[0] - requested, .5)
                    if not fail_force:
                        self.assertIn('force-terminated', '\n'.join(state['logs']))
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                with patch.object(review, '_entries_fingerprint', return_value='source'):
                    resumed = review.load_checkpoint(str(output), 2, 1, 0, rows)
                self.assertEqual(1, resumed['completed_batches'])
                self.assertEqual(rows, resumed['all_corrected'])
            finally:
                timer.cancel()
                timer.join(2)
                for process in processes:
                    if is_subprocess_tree_running(process):
                        real_terminate(process._alexandria_windows_owner.job)
                thread.join(5)
                for process in processes:
                    self._close_owned(process)

    def test_review_cooperative_cancel_stops_real_headless_tree(self):
        self._cancel_review_tree(cooperative=True)

    def test_review_force_cancel_stops_real_detached_tree_and_keeps_checkpoint(self):
        self._cancel_review_tree(cooperative=False)

    def test_review_force_failure_is_not_reported_as_cancelled(self):
        self._cancel_review_tree(cooperative=False, fail_force=True)

    def _start_owned_sleep(self, termination_grace=1, **extra):
        return start_owned_subprocess(
            [_base_python(), "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
            termination_grace=termination_grace,
            **extra,
        )

    def _close_owned(self, proc):
        control = getattr(proc, "_alexandria_control", None)
        if control is not None:
            control.close()
        proc.wait(timeout=3)
        if proc.stdout is not None:
            proc.stdout.close()

    def test_cancel_refusal_acknowledges_live_job_then_escalates(self):
        import core
        proc = self._start_owned_sleep()
        state = dict(running=True, cancel=False, process=proc, pid=proc.pid)
        try:
            time.sleep(.3)
            with patch.dict(core.process_state, {'_native_refusal': state}), \
                 self.assertLogs('AlexandriaUI', level='WARNING') as captured:
                result = core._cancel_task('_native_refusal', 'idle', 'exited')
            self.assertEqual('cancel requested', result['status'])
            warnings = [record for record in captured.records
                        if record.getMessage().startswith('Cancellation requested;')]
            self.assertEqual(1, len(warnings))
            self.assertIsNone(warnings[0].exc_info)
            self.assertIn('taskkill exit', '\n'.join(captured.output))
            self.assertIn('waiting for the worker to stop', state['logs'][-1])
            self.assertTrue(state['cancel'])
            self.assertTrue(is_subprocess_tree_running(proc))
            with patch.object(core, 'CANCEL_TERMINATE_GRACE_SECONDS', .1):
                stamp, killed = core.apply_cancel_escalation(proc, None, False)
                self.assertFalse(killed)
                time.sleep(.15)
                _, killed = core.apply_cancel_escalation(proc, stamp, killed)
            self.assertTrue(killed)
            proc._alexandria_windows_owner.job.ensure_stopped()
            proc.wait(timeout=3)
            self.assertFalse(is_subprocess_tree_running(proc))
        finally:
            self._close_owned(proc)

    def test_capture_timeout_survives_real_graceful_refusal_and_reaps_job(self):
        import subprocess_ownership as ownership
        processes = []
        def start(*args, **kwargs):
            process = start_owned_subprocess(*args, **kwargs)
            processes.append(process)
            return process
        try:
            with patch.object(ownership, 'start_owned_subprocess', side_effect=start):
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    run_owned_capture([_base_python(), '-c', 'import time; time.sleep(30)'], timeout=.3)
            self.assertEqual(.3, raised.exception.timeout)
            self.assertEqual(1, len(processes))
            process = processes[0]
            self.assertIsNotNone(process.poll())
            self.assertIsNone(process._alexandria_windows_owner.job.handle)
            self.assertFalse(is_subprocess_tree_running(process))
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
        finally:
            for process in processes:
                self._close_owned(process)
                if process.stderr is not None:
                    process.stderr.close()

    def test_stream_force_failures_reach_owned_cleanup_and_preserve_error(self):
        import core
        from windows_subprocess_job import WindowsSubprocessJob
        real_terminate = WindowsSubprocessJob.terminate
        unrelated = subprocess.Popen([_base_python(), '-c', 'import time; time.sleep(30)'],
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for failures in (1, 2, 4):
                with self.subTest(failures=failures):
                    processes, timers, attempts = [], [], []
                    watchdog = threading.Event()
                    state = dict(logs=[], cancel=True, process=None, pid=None, processes=[])
                    def emergency_stop(job):
                        watchdog.set()
                        real_terminate(job)
                    def start(*args, **kwargs):
                        process = start_owned_subprocess(*args, **kwargs)
                        processes.append(process)
                        timer = threading.Timer(8, emergency_stop,
                                                args=(process._alexandria_windows_owner.job,))
                        timers.append(timer)
                        timer.start()
                        return process
                    def terminate(job, exit_code=1):
                        self.assertIsNotNone(job.handle)
                        self.assertGreater(job.get_active_process_count(), 0)
                        attempts.append(job.handle)
                        if len(attempts) <= failures:
                            raise OSError(f'injected force failure {len(attempts)}')
                        return real_terminate(job, exit_code)
                    try:
                        with patch.object(core, 'start_owned_subprocess', side_effect=start), \
                             patch.object(core, 'CANCEL_TERMINATE_GRACE_SECONDS', .05), \
                             patch.object(WindowsSubprocessJob, 'terminate', new=terminate):
                            with self.assertRaisesRegex(OSError, 'injected force failure 1') as raised:
                                core._stream_subprocess_to_logs(
                                    [_base_python(), '-c', 'import time; time.sleep(30)'], os.getcwd(), state)
                        self.assertNotIsInstance(raised.exception, core.WindowsOwnerCleanupError)
                        self.assertEqual(failures + 1, len(attempts))
                        self.assertFalse(watchdog.is_set(), 'stream cancellation hung until watchdog')
                        self.assertEqual(1, len(set(attempts)), 'cleanup changed owned jobs')
                        self.assertIsNone(processes[0]._alexandria_windows_owner.job.handle)
                        self.assertFalse(is_subprocess_tree_running(processes[0]))
                        self.assertIsNotNone(processes[0].poll())
                        self.assertTrue(processes[0].stdout.closed)
                        self.assertIsNone(state['process'])
                        self.assertIsNone(state['pid'])
                        self.assertEqual([], state['processes'])
                        self.assertIsNone(unrelated.poll())
                    finally:
                        for timer in timers:
                            timer.cancel()
                            timer.join(2)
                        for process in processes:
                            self._close_owned(process)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_codeless_force_failure_deadline_preserves_cause_and_live_ownership(self):
        import windows_subprocess_owner as owner
        process = self._start_owned_sleep()
        control = process._alexandria_windows_owner
        handle = control.job.handle
        failure = OSError('injected persistent force failure')
        try:
            with patch.object(control.job, 'terminate', side_effect=failure) as terminate, \
                 patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', .15):
                started = time.monotonic()
                with self.assertRaisesRegex(owner.WindowsOwnerCleanupError,
                        'deadline exceeded.*Win32 error None.*not proven dead') as raised:
                    control.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertGreater(terminate.call_count, 1)
            self.assertIs(failure, raised.exception.__cause__)
            self.assertIs(control, raised.exception.owner)
            self.assertIs(raised.exception, control.cleanup_error)
            self.assertIn(control, owner._retained_windows_owners)
            self.assertEqual(handle, control.job.handle)
            self.assertGreater(control.job.get_active_process_count(), 0)
            self.assertTrue(is_subprocess_tree_running(process))
            self.assertIsNone(process.poll())
            self.assertTrue(Path(control.directory.name).is_dir())
        finally:
            self._close_owned(process)

    def test_verifier_stop_process_group_honors_full_grace_then_forces(
        self,
    ):
        unrelated = subprocess.Popen(
            [_base_python(), "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc = None
        try:
            proc = self._start_owned_sleep(termination_grace=1)
            time.sleep(0.3)
            self.assertTrue(is_subprocess_tree_running(proc))
            started = time.monotonic()
            # Must not raise the spurious Windows taskkill OSError that
            # masks the real result. Must honor the full 1s grace (no
            # early force at ~0.08s) before the exact Job force tail.
            verifier.stop_process_group(proc, timeout=1)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.7, "forced before full grace")
            self.assertLess(elapsed, 3.0, "did not bound cleanup")
            self.assertFalse(
                is_subprocess_tree_running(proc),
                "owned tree still running after stop",
            )
            self.assertIsNotNone(proc.poll(), "supervisor not reaped")
            self.assertIsNone(
                unrelated.poll(), "unrelated process was signaled"
            )
        finally:
            if proc is not None:
                self._close_owned(proc)
            if unrelated.poll() is None:
                unrelated.terminate()
                unrelated.wait(timeout=3)
            else:
                unrelated.wait(timeout=3)

    def test_isolated_server_finally_does_not_mask_success(self):
        # Stand in for the uvicorn server with an owned headless sleeper.
        # Readiness + suite are mocked; only the finally stop/close under
        # test is real. Before the fix the graceful taskkill OSError with
        # no original error re-raises and main() fails despite suite rc 0.
        unrelated = subprocess.Popen(
            [_base_python(), "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        server = None
        try:
            server = self._start_owned_sleep(termination_grace=1)
            time.sleep(0.3)
            # Keep native taskkill and Job cleanup real while shortening
            # the runner's ten-second grace for this disposable fixture.
            real_stop = runner.stop_owned_subprocess
            real_run = runner.subprocess.run

            def bounded_stop(process, timeout=10, **kwargs):
                # Keep the caller's escalation policy (force flag must be
                # preserved) but bound grace to 1s for a focused test.
                return real_stop(process, timeout=1, **kwargs)

            def suite_or_real_run(command, **kwargs):
                # Only the suite call is faked. The graceful taskkill
                # subprocess.run must stay real or this test mocks away
                # the very failure it reproduces.
                if "tests.test_api" in [str(c) for c in command]:
                    return SimpleNamespace(returncode=0)
                return real_run(command, **kwargs)

            class _CM:
                def __enter__(self):
                    return None

                def __exit__(self, *a):
                    return False

            with (
                patch.object(
                    runner,
                    "start_owned_subprocess",
                    return_value=server,
                ),
                patch.object(
                    runner, "get_isolated_server_port", return_value=18765
                ),
                patch.object(
                    runner,
                    "urlopen",
                    return_value=_CM(),
                ),
                patch.object(
                    runner.subprocess,
                    "run",
                    side_effect=suite_or_real_run,
                ),
                patch.object(runner, "stop_owned_subprocess", side_effect=bounded_stop),
                patch.object(
                    runner,
                    "use_private_cpu_fixture_lock",
                    return_value=contextlib.nullcontext(),
                ),
            ):
                result = runner.main([])
            self.assertEqual(0, result, "cleanup masked the passing suite")
            self.assertFalse(is_subprocess_tree_running(server))
            self.assertIsNotNone(server.poll())
            self.assertIsNone(unrelated.poll(), "unrelated process was signaled")
        finally:
            if server is not None:
                self._close_owned(server)
            if unrelated.poll() is None:
                unrelated.terminate()
                unrelated.wait(timeout=3)
            else:
                unrelated.wait(timeout=3)

    def test_job_graceful_request_still_reports_headless_failure_loudly(self):
        # Guard against over-suppression: the Job-layer graceful request
        # itself must still raise (not silently succeed) for a headless
        # console child. The caller-level escalation must not change that.
        from windows_subprocess_job import WindowsSubprocessJob

        child = subprocess.Popen(
            [_base_python(), "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job = WindowsSubprocessJob()
        try:
            job.apply_process_membership(int(child._handle))
            with self.assertRaisesRegex(
                OSError, "Windows taskkill failed for job member"
            ):
                job.request_termination(owner_pid=999999, timeout=2)
            job.terminate()
            job.ensure_stopped()
            self.assertEqual(0, job.get_active_process_count())
            child.wait(timeout=5)
        finally:
            job.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def test_detached_descendant_is_stopped_by_job_force_tail(self):
        # Kernel-membership proof: a detached descendant (new session) must
        # not survive the Job force tail. Worker records its child pid.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            worker = (
                "import subprocess,sys,time,pathlib;"
                "c=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)'],start_new_session=True,"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "pathlib.Path(sys.argv[1]).write_text(str(c.pid));"
                "time.sleep(30)"
            )
            unrelated = subprocess.Popen(
                [_base_python(), "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            proc = None
            try:
                proc = start_owned_subprocess(
                    [_base_python(), "-c", worker, str(pid_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                    termination_grace=1,
                )
                deadline = time.monotonic() + 10
                child_pid = None
                while time.monotonic() < deadline:
                    if pid_path.exists():
                        child_pid = int(pid_path.read_text())
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child_pid, "detached child not started")
                job = proc._alexandria_windows_owner.job
                self.assertIn(child_pid, job.get_process_ids(), 'child was never admitted')
                verifier.stop_process_group(proc, timeout=1)
                self.assertEqual([], job.get_process_ids())
                self.assertEqual(0, job.get_active_process_count())
                self.assertFalse(is_subprocess_tree_running(proc))
                self.assertIsNone(unrelated.poll())
            finally:
                if proc is not None:
                    self._close_owned(proc)
                if unrelated.poll() is None:
                    unrelated.terminate()
                    unrelated.wait(timeout=3)
                else:
                    unrelated.wait(timeout=3)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WindowsHeadlessCleanupEscalationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 11 and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
