"""Mocked Win32 failures exercise bounded cleanup and real task lifecycle."""
import asyncio
import copy
import functools
import io
import json
from pathlib import Path
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
        timeout = .08
        with patch.object(owner.time, 'monotonic', self.real_monotonic), \
             patch.object(owner.time, 'sleep', self.real_sleep), \
             patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', timeout), \
             patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner.logging, 'exception'), \
             patch('review_startup.record_failure'):
            started = time.perf_counter()
            deadline = self.real_monotonic() + timeout
            with self.assertRaisesRegex(OSError, 'deadline exceeded.*Win32 error 170'):
                self.control.close()
            self.assertGreaterEqual(self.real_monotonic(), deadline)
            elapsed = time.perf_counter() - started
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

    def test_benchmark_cancel_keeps_process_registered_when_close_fails(self):
        import benchmark_execution as execution
        state = {'cancel': False, 'processes': []}
        def start(*args, **kwargs):
            state['cancel'] = True
            return self.process
        token = execution.BENCHMARK_STATE.set(state)
        try:
            with patch.object(execution, 'start_owned_subprocess', side_effect=start), \
                 patch.object(execution, 'get_benchmark_process_options', return_value={}), \
                 patch.object(owner.logging, 'exception'), \
                 self.assertRaises(owner.WindowsOwnerCleanupError):
                execution.run_benchmark_subprocess(['synthetic-worker'])
            self.assertIn(self.process, state['processes'])
            self.assertIsNotNone(self.job.handle)
        finally:
            execution.BENCHMARK_STATE.reset(token)

    def test_benchmark_cancel_cleanup_failure_retains_task_and_gpu_leases(self):
        import benchmark_execution as execution
        import benchmark_runner as runner
        for batched in (False, True):
            with self.subTest(batched=batched):
                states = {'benchmark': dict(running=True, cancel=False, status='running',
                                           processes=[], process=None, logs=[], tasks=[])}
                state = states['benchmark']
                lease, gpu = Mock(), object()
                claims = {'benchmark': {'id': 'benchmark-owner', 'phase': 'pending', 'lease': lease}}
                manifest = {'stage': 'synthetic', 'fixtures': [{'id': 'one'}], 'repetitions': 1}
                def start(*args, **kwargs):
                    state['cancel'] = True
                    return self.process
                def execute(*args):
                    return execution.run_benchmark_subprocess(['synthetic-worker'])
                loop = runner.run_benchmark_batch if batched else runner.run_benchmark_repetitions
                stage = runner.wrap_benchmark_cancellation(
                    lambda manifest, environment, path, state: loop(
                        manifest, environment, path, state, execute, report={'cases': []}))
                with patch.object(core, 'process_state', states), \
                     patch.object(core, '_task_claims', claims), \
                     patch.object(core, '_gpu_leases', {'benchmark': gpu}), \
                     patch.object(core, 'release_gpu_lock') as release, \
                     patch.object(execution, 'get_benchmark_process_options', return_value={}), \
                     patch.object(execution, 'start_owned_subprocess', side_effect=start), \
                     patch.object(runner, 'apply_benchmark_cancellation', side_effect=lambda *args: state['cancel']) as cancel, \
                     patch.object(owner.logging, 'exception'), \
                     self.assertRaises(owner.WindowsOwnerCleanupError):
                    try:
                        core._run_owned_background_task('benchmark', 'benchmark-owner',
                            lambda: stage(manifest, {}, 'unused-report', state))
                    finally:
                        self.assertEqual('failed', state['status'])
                        self.assertFalse(state['running'])
                        self.assertTrue(state.get('_windows_cleanup_failed'))
                        self.assertIn(self.process, state['processes'])
                        self.assertEqual('cleanup_failed', claims['benchmark']['phase'])
                        self.assertTrue(core.is_task_running('benchmark'))
                        self.assertIn('Win32 error 5', '\n'.join(state['logs']))
                        release.assert_not_called()
                        lease.close.assert_not_called()
                        self.assertEqual(1, cancel.call_count)

    def test_cancellable_client_preserves_cleanup_error_despite_cancel(self):
        import benchmark_execution as execution
        state = {'cancel': False}
        failure = owner.WindowsOwnerCleanupError(self.control, OSError(5, 'denied'))
        resource = Mock()
        def request():
            state['cancel'] = True
            raise failure
        resource.create.side_effect = request
        guarded = execution._BenchmarkResource(resource, state)
        with self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            guarded.create()
        self.assertIs(failure, raised.exception)

    def test_run_process_propagates_cleanup_failure_before_training_cancel_fallback(self):
        state = {'running': True, 'cancel': True, 'status': 'running', 'logs': [], 'processes': []}
        failure = owner.WindowsOwnerCleanupError(self.control, OSError(5, 'denied'))
        with patch.object(core, 'process_state', {'lora_training': state}), \
             patch.object(core, '_init_task_log', return_value=None), \
             patch.object(core, '_stream_subprocess_to_logs', side_effect=failure), \
             self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            core.run_process(['synthetic-training'], 'lora_training')
        self.assertIs(failure, raised.exception)
        self.assertEqual('failed', state['status'])
        self.assertTrue(state['_windows_cleanup_failed'])
        self.assertIn(self.process, state['processes'])

    def test_dataset_generation_never_masks_cleanup_failure_or_releases_leases(self):
        from routers import dataset_builder as routes
        for batched in (False, True):
            with self.subTest(batched=batched), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary) / 'voice'
                work.mkdir()
                (work / 'state.json').write_text(json.dumps({'samples': [{'text': 'One'}]}))
                state = {'running': True, 'cancel': False, 'logs': [], 'processes': []}
                states = {'dataset_builder': state}
                lease, gpu = Mock(), object()
                claims = {'dataset_builder': {'id': 'dataset-owner', 'phase': 'pending', 'lease': lease}}
                failure = owner.WindowsOwnerCleanupError(self.control, OSError(5, 'denied'))
                engine = Mock()
                engine.generate_voice_design.side_effect = failure
                def start(name, claim_id, callback, *args, **kwargs):
                    return core._run_owned_background_task(
                        name, claim_id, functools.partial(callback, *args, **kwargs))
                with patch.object(core, 'process_state', states), \
                     patch.object(routes, 'process_state', states), \
                     patch.object(core, '_task_claims', claims), \
                     patch.object(core, '_gpu_leases', {'dataset_builder': gpu}), \
                     patch.object(core, 'release_gpu_lock') as release, \
                     patch.object(routes, 'DATASET_BUILDER_DIR', temporary), \
                     patch.object(routes, 'check_global_gpu_lock'), \
                     patch.object(routes, 'claim_gpu_task', return_value='dataset-owner'), \
                     patch.object(routes, 'reserve_background_task', return_value='dataset-owner'), \
                     patch.object(routes, 'start_claimed_task_thread', side_effect=start), \
                     patch.object(routes.project_manager, 'get_engine', return_value=engine), \
                     self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
                    try:
                        if batched:
                            request = routes.DatasetBatchGenRequest(name='voice', description='Warm',
                                samples=[{'text': 'One'}], indices=[0])
                            asyncio.run(routes.dataset_builder_generate_batch(request))
                        else:
                            request = routes.DatasetSampleGenRequest(dataset_name='voice', sample_index=0,
                                description='Warm', text='One', seed=0)
                            routes._generate_dataset_sample_sync(request)
                    finally:
                        self.assertEqual('failed', state.get('status'))
                        self.assertTrue(state.get('_windows_cleanup_failed'))
                        self.assertIn(self.process, state['processes'])
                        self.assertFalse(any('[DONE]' in line for line in state['logs']))
                        release.assert_not_called()
                        lease.close.assert_not_called()
                self.assertIs(failure, raised.exception)

    def test_retained_retry_is_throttled_and_never_closes_live_job(self):
        with patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', 0), \
             patch.object(owner.logging, 'exception'), \
             self.assertRaises(owner.WindowsOwnerCleanupError):
            self.control.close()
        original = self.control.cleanup_error
        self.api.fail = None
        self.control.retry_cleanup()
        self.assertEqual(1, sum(call[0] == 'terminate' for call in self.api.calls))
        self.clock += 30
        self.control.retry_cleanup()
        self.control.retry_cleanup()
        self.assertEqual(2, sum(call[0] == 'terminate' for call in self.api.calls))
        self.assertIs(original, self.control.cleanup_error)
        self.assertIsNotNone(self.job.handle)
        self.directory.cleanup.assert_not_called()
        self.clock += 30
        self.control.retry_cleanup()
        self.assertEqual(3, sum(call[0] == 'terminate' for call in self.api.calls))

    def test_retained_access_denied_never_retries_termination(self):
        with patch.object(owner.logging, 'exception'), self.assertRaises(owner.WindowsOwnerCleanupError):
            self.control.close()
        self.clock += 60
        self.control.retry_cleanup()
        self.assertEqual(1, sum(call[0] == 'terminate' for call in self.api.calls))
        self.assertIsNotNone(self.job.handle)

    def test_empty_retained_job_close_uses_the_shared_retry_interval(self):
        self.api.counts = [0]
        self.api.fail = None
        failure = OSError(6, 'protected job handle')
        failure.winerror = 6
        real_close = self.job.close
        with patch.object(self.job, 'close', side_effect=failure) as close, \
             patch.object(owner.logging, 'exception'):
            with self.assertRaises(owner.WindowsOwnerCleanupError):
                self.control.close()
            self.control.retry_cleanup()
            self.clock = 29
            self.control.retry_cleanup()
            self.assertEqual(1, close.call_count)
            self.clock = 30
            with self.assertRaises(owner.WindowsOwnerCleanupError):
                self.control.retry_cleanup()
            self.assertEqual(2, close.call_count)
            close.side_effect = real_close
            self.clock = 59
            self.control.retry_cleanup()
            self.assertEqual(2, close.call_count)
            self.assertIsNotNone(self.job.handle)
            self.directory.cleanup.assert_not_called()
            self.clock = 60
            self.control.retry_cleanup()
        self.assertIsNone(self.job.handle)
        self.directory.cleanup.assert_called_once_with()

    def test_unknown_close_failure_gets_one_recovery_attempt_per_interval(self):
        self.api.counts = [0]
        self.api.fail = None
        with patch.object(self.job, 'close', side_effect=OSError('unknown close failure')) as close, \
             patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', 0), \
             patch.object(owner.logging, 'exception'):
            with self.assertRaises(owner.WindowsOwnerCleanupError):
                self.control.close()
            self.clock = 30
            with self.assertRaises(owner.WindowsOwnerCleanupError):
                self.control.retry_cleanup()
            self.control.retry_cleanup()
            self.assertEqual(2, close.call_count)
            self.assertEqual([], self.delays)
            self.assertIsNotNone(self.job.handle)

    def test_retained_retry_can_fail_permanently_without_losing_cause(self):
        with patch.object(jobs.ctypes, 'get_last_error', return_value=170), \
             patch.object(owner, 'WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS', 0), \
             patch.object(owner.logging, 'exception'), self.assertRaises(owner.WindowsOwnerCleanupError):
            self.control.close()
        self.clock += 30
        with patch.object(jobs.ctypes, 'get_last_error', return_value=5), \
             self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            self.control.retry_cleanup()
        self.assertEqual(5, raised.exception.__cause__.errno)
        self.assertIs(raised.exception, self.control.cleanup_error)
        self.clock += 30
        self.control.retry_cleanup()
        self.assertEqual(2, sum(call[0] == 'terminate' for call in self.api.calls))

    def test_reaper_retries_live_owner_and_releases_only_after_zero_membership(self):
        with patch.object(owner.logging, 'exception'), self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            self.control.close()
        state = {'processes': [], 'logs': []}
        core._retain_windows_owner_failure(state, raised.exception)
        lease, gpu = Mock(), object()
        claims = {'benchmark': {'id': 'recovery-owner', 'phase': 'cleanup_failed', 'lease': lease}}
        observations = []
        def recover():
            observations.append(self.job.get_active_process_count())
            release.assert_not_called()
            lease.close.assert_not_called()
            self.assertIsNotNone(self.job.handle)
            self.api.fail = None
            self.api.counts = [0]
            self.control.close()
        with patch.object(core, 'process_state', {'benchmark': state}), \
             patch.object(core, '_task_claims', claims), \
             patch.object(core, '_gpu_leases', {'benchmark': gpu}), \
             patch.object(core, 'release_gpu_lock') as release, \
             patch.object(self.control, 'retry_cleanup', side_effect=recover):
            core._reap_gpu_leases()
        self.assertEqual([1], observations)
        self.assertEqual({}, claims)
        self.assertIsNone(self.job.handle)
        release.assert_called_once_with(gpu)
        lease.close.assert_called_once_with()

    def test_cleanup_failure_names_members_and_manual_recovery(self):
        with patch.object(owner.logging, 'exception'), self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            self.control.close()
        message = str(raised.exception)
        self.assertIn('100, 101, 102', message)
        self.assertIn('Task Manager', message)
        self.assertIn('restart the app', message)
        self.assertIn('automatically', message)

    def test_membership_query_failure_remains_actionable_without_claiming_dead(self):
        self.api.fail = 'query'
        with patch.object(jobs.ctypes, 'get_last_error', return_value=6), \
             patch.object(owner.logging, 'exception'), self.assertRaises(owner.WindowsOwnerCleanupError) as raised:
            self.control.close()
        self.assertIn('PIDs unavailable', str(raised.exception))
        self.assertIn('not proven dead', str(raised.exception))
        self.assertIsNotNone(self.job.handle)

    def test_failure_pid_diagnostics_bound_repeated_membership_resize(self):
        query = self.api.QueryInformationJobObject
        def changing_membership(handle, kind, address, size, returned):
            result = query(handle, kind, address, size, returned)
            return False if kind == 3 else result
        with patch.object(self.api, 'QueryInformationJobObject', side_effect=changing_membership), \
             patch.object(jobs.ctypes, 'get_last_error', return_value=234):
            failure = owner.WindowsOwnerCleanupError(self.control, OSError(5, 'denied'))
        self.assertEqual(8, sum(call[0] == 'query' and call[2] == 3 for call in self.api.calls))
        self.assertEqual(5, failure.errno)
        self.assertIn('PIDs unavailable', str(failure))
        self.assertIsNotNone(self.job.handle)

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
            self.clock = self.control._cleanup_retry_at
            core._reap_gpu_leases()
            self.assertNotIn(task_name, claims)
            self.assertIsNone(task['process'])
            self.assertFalse(core.is_task_running(task_name))
            self.assertNotIn(self.control, owner._retained_windows_owners)
            release.assert_called_once_with(gpu)
            lease.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
