"""Mocked Win32 failures exercise bounded cleanup and real task lifecycle."""
import asyncio
import copy
import io
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import core
import windows_subprocess_job as jobs
import windows_subprocess_owner as owner
from tests.test_windows_subprocess_job import JobApiFixture


class WindowsOwnerCloseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.real_monotonic = time.monotonic
        self.real_sleep = time.sleep
        self.api = JobApiFixture()
        self.api.counts = [1]
        self.api.fail = 'terminate'
        self.clock = 0
        self.delays = []
        self.directory = Mock()
        self.process = Mock(pid=9001, stdout=io.StringIO(''), returncode=0)
        self.process.wait.return_value = 0
        self.process.poll.return_value = 0
        self.patches = [
            patch.object(jobs, 'get_windows_job_api', return_value=self.api),
            patch.object(jobs.ctypes, 'get_last_error', return_value=5, create=True),
            patch.object(jobs.ctypes, 'FormatError', return_value='synthetic failure', create=True),
            patch.object(owner.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(owner.time, 'sleep', side_effect=self.advance),
        ]
        for patched in self.patches:
            patched.start()
            self.addCleanup(patched.stop)
        self.job = jobs.WindowsSubprocessJob()
        self.control = owner.WindowsOwnerControl(self.job, self.process, self.directory)
        self.process._alexandria_control = self.control
        self.process._alexandria_windows_owner = self.control
        self.addCleanup(self.retire)

    def advance(self, delay):
        self.delays.append(delay)
        self.clock += delay
        if self.clock > 11:
            raise AssertionError('close exceeded its ten-second cleanup deadline')

    def retire(self):
        self.api.fail = None
        self.api.counts = [0]
        self.control.close()

    def test_permanent_termination_failure_is_bounded_and_retains_ownership(self):
        started = time.perf_counter()
        with self.assertRaisesRegex(OSError, 'Win32 error 5.*not proven dead'), \
             patch.object(owner.logging, 'exception'), \
             patch('review_startup.record_failure') as diagnostic:
            self.control.close()
        self.assertLess(time.perf_counter() - started, 1)
        self.assertLessEqual(self.clock, 10)
        self.assertEqual([], self.delays)
        self.assertEqual(1, sum(call[0] == 'terminate' for call in self.api.calls))
        self.assertEqual(self.api.handle, self.job.handle)
        self.assertFalse(any(call[0] == 'close' for call in self.api.calls))
        self.directory.cleanup.assert_not_called()
        self.assertIn(self.control, owner._retained_windows_owners)
        self.assertEqual('windows_owner_cleanup_failed', diagnostic.call_args.args[0])
        self.assertEqual(5, diagnostic.call_args.args[1].errno)

    def test_invalid_handle_query_failure_retains_unknown_membership(self):
        self.api.fail = 'query'
        with patch.object(jobs.ctypes, 'get_last_error', return_value=6), \
             self.assertRaisesRegex(OSError, 'Win32 error 6.*not proven dead'), \
             patch.object(owner.logging, 'exception'):
            self.control.close()
        self.assertFalse(any(call[0] in ('close', 'terminate') for call in self.api.calls))
        self.assertIsNotNone(self.job.handle)
        self.directory.cleanup.assert_not_called()
        self.assertEqual([], self.delays)

    def test_transient_failure_returns_within_real_wall_clock_deadline(self):
        with patch.object(owner.time, 'monotonic', self.real_monotonic), \
             patch.object(owner.time, 'sleep', self.real_sleep), \
             patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', .08), \
             patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner.logging, 'exception'), \
             patch('review_startup.record_failure'):
            started = time.perf_counter()
            with self.assertRaisesRegex(OSError, 'deadline exceeded.*Win32 error 170'):
                self.control.close()
            elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, .08)
        self.assertLess(elapsed, 1)

    def test_transient_failure_exhausts_deadline_with_backoff_and_retains_ownership(self):
        with patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner.logging, 'exception'), \
             self.assertRaisesRegex(OSError, 'deadline exceeded.*Win32 error 170.*not proven dead'):
            self.control.close()
        self.assertEqual(10, self.clock)
        self.assertGreater(len(self.delays), 1)
        self.assertGreater(self.delays[-1], self.delays[0])
        self.assertIsNotNone(self.job.handle)
        self.directory.cleanup.assert_not_called()

    def test_invalid_parameter_is_not_retried(self):
        with patch.object(jobs.ctypes, 'get_last_error', return_value=87), \
             patch.object(owner.logging, 'exception'), \
             self.assertRaisesRegex(OSError, 'failed without retry.*Win32 error 87'):
            self.control.close()
        self.assertEqual([], self.delays)
        self.assertIsNotNone(self.job.handle)

    def test_codeless_termination_failure_retries_then_proves_zero_membership(self):
        failure = OSError('injected unknown termination failure')
        attempts = []
        def terminate():
            attempts.append(self.job.handle)
            if len(attempts) == 1:
                raise failure
            self.api.counts = [0]
        with patch.object(self.job, 'terminate', side_effect=terminate), \
             patch.object(owner.logging, 'exception'):
            self.control.close()
        self.assertEqual([.05], self.delays)
        self.assertEqual(2, len(attempts))
        self.assertIsNone(self.job.handle)
        self.directory.cleanup.assert_called_once_with()

    def test_codeless_failure_exhausts_deadline_and_preserves_cause_and_ownership(self):
        failure = OSError('injected unknown termination failure')
        with patch.object(self.job, 'terminate', side_effect=failure) as terminate, \
             patch.object(owner.logging, 'exception'), \
             self.assertRaisesRegex(owner.WindowsOwnerCleanupError,
                                    'deadline exceeded.*Win32 error None.*not proven dead') as raised:
            self.control.close()
        self.assertIs(failure, raised.exception.__cause__)
        self.assertEqual(10, self.clock)
        self.assertGreater(terminate.call_count, 1)
        self.assertIn(self.control, owner._retained_windows_owners)
        self.assertIs(self.control, raised.exception.owner)
        self.assertIsNotNone(self.job.handle)
        self.assertFalse(any(call[0] == 'close' for call in self.api.calls))
        self.directory.cleanup.assert_not_called()

    def test_unrecognized_numeric_error_uses_bounded_retry(self):
        with patch.object(jobs.ctypes, 'get_last_error', return_value=9999), \
             patch.object(owner.logging, 'exception'), \
             self.assertRaisesRegex(owner.WindowsOwnerCleanupError,
                                    'deadline exceeded.*Win32 error 9999'):
            self.control.close()
        self.assertEqual(10, self.clock)
        self.assertGreater(len(self.delays), 1)
        self.assertIsNotNone(self.job.handle)

    def test_transient_termination_failure_retries_then_proves_zero_membership(self):
        terminate = self.api.TerminateJobObject
        attempts = []
        def retry(handle, exit_code):
            attempts.append(handle)
            if len(attempts) == 2:
                self.api.fail = None
                self.api.counts = [1, 0]
            return terminate(handle, exit_code)
        with patch.object(self.api, 'TerminateJobObject', side_effect=retry), \
             patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner.logging, 'exception'):
            self.control.close()
        self.assertEqual(2, len(attempts))
        self.assertEqual([.05, .05], self.delays)
        self.assertIsNone(self.job.handle)
        self.directory.cleanup.assert_called_once_with()

    def test_successful_termination_without_zero_membership_is_also_bounded(self):
        self.api.fail = None
        with self.assertRaisesRegex(OSError, 'not proven dead'), \
             patch.object(owner.logging, 'exception'):
            self.control.close()
        self.assertLessEqual(self.clock, 10)
        self.assertIsNotNone(self.job.handle)
        self.directory.cleanup.assert_not_called()

    def test_failed_task_retains_gpu_and_task_leases_until_confirmed_shutdown(self):
        self.check_failed_task()

    def test_codeless_failure_marks_task_failed_and_retains_leases(self):
        with patch.object(self.job, 'terminate',
                          side_effect=OSError('injected unknown termination failure')):
            self.check_failed_task(error_code=None)

    def test_failed_admission_retains_gpu_and_task_leases_until_confirmed_shutdown(self):
        self.check_failed_task(admission=True)

    def test_published_script_cleanup_failure_never_reports_success(self):
        self.check_failed_task(task_name='script', published=True)

    def check_failed_task(self, admission=False, task_name='review', published=False, error_code=5):
        state = copy.deepcopy(core.process_state)
        for task in state.values():
            task['running'] = False
        state[task_name].update(status='running', processes=[], process=None, pid=None)
        lease, gpu = Mock(), object()
        claims = {task_name: {'id': 'synthetic-owner', 'phase': 'pending', 'lease': lease}}
        def start(*args, **kwargs):
            if published:
                state[task_name]['_script_published_cleanup_stop'] = True
            if admission:
                self.control.close()
            return self.process
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(core, 'process_state', state), \
             patch.object(core, '_task_claims', claims), \
             patch.object(core, '_gpu_leases', {task_name: gpu}), \
             patch.object(core, 'release_gpu_lock') as release, \
             patch.object(core, '_init_task_log', return_value=None), \
             patch.object(core, 'RUN_HISTORY_DIR', temporary), \
             patch.object(core, 'get_gpu_task_environment', return_value={}), \
             patch.object(core, 'get_task_lease_descriptor', return_value=None), \
             patch.object(core, 'start_owned_subprocess', side_effect=start), \
             patch.object(owner.logging, 'exception'), \
             patch.object(core, 'logger'), \
             patch.object(core, 'record_failure'):
            core._run_owned_background_task(task_name, 'synthetic-owner',
                lambda: core._run_claimed_background_task(task_name,
                    lambda: core.run_process(['synthetic-worker'], task_name, temporary)))
            task = state[task_name]
            self.assertFalse(task['running'])
            self.assertEqual('failed', task['status'])
            self.assertIn(f'Win32 error {error_code}', '\n'.join(task['logs']))
            self.assertIn('not proven dead', '\n'.join(task['logs']))
            self.assertIs(self.process, task['process'])
            self.assertIn(self.process, task['processes'])
            self.assertTrue(core.is_task_running(task_name))
            from routers import script
            with patch.object(script, 'process_state', state):
                status = asyncio.run(script.get_status(task_name))
                self.assertFalse(status['running'])
                self.assertEqual('failed', status['status'])
                self.assertTrue(status['ownership_unconfirmed'])
                self.assertFalse(asyncio.run(script.get_task_statuses())[task_name]['running'])
            self.assertNotIn('completed successfully', '\n'.join(task['logs']))
            with self.assertRaises(core.HTTPException):
                core.check_global_gpu_lock(task_name)
            with self.assertRaises(core.WindowsOwnerCleanupError):
                core._stream_subprocess_to_logs(['another-worker'], temporary, task)
            release.assert_not_called()
            lease.close.assert_not_called()
            self.assertFalse(core.release_gpu_task_claim(task_name, 'synthetic-owner'))
            self.api.fail = None
            self.api.counts = [0]
            core._reap_gpu_leases()
            self.assertNotIn(task_name, claims)
            self.assertIsNone(task['process'])
            self.assertFalse(core.is_task_running(task_name))
            self.assertNotIn(self.control, owner._retained_windows_owners)
            release.assert_called_once_with(gpu)
            lease.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
