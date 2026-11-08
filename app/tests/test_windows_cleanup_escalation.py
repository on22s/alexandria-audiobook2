"""Cross-platform policy and failure routing for qualification cleanup."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import core
import verify_release as verifier
import subprocess_ownership as ownership
from windows_subprocess_owner import WindowsOwnerControl


class GracefulRefusalTests(unittest.TestCase):
    def owned_process(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        job = Mock(handle=456)
        job.get_active_process_count.return_value = 1
        job.request_termination.side_effect = OSError('graceful refusal')
        job.terminate.side_effect = lambda: setattr(job.get_active_process_count, 'return_value', 0)
        job.close.side_effect = lambda: setattr(job, 'handle', None)
        control = WindowsOwnerControl(job, process, Mock(), termination_grace=0)
        process._alexandria_control = process._alexandria_windows_owner = control
        return process, job

    def test_owned_windows_graceful_refusal_still_forces_and_waits(self):
        process, job = self.owned_process()
        with patch.object(ownership.sys, 'platform', 'win32'):
            ownership.stop_owned_subprocess(process, timeout=0)
        job.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0)
        self.assertFalse(ownership.is_subprocess_tree_running(process))

    def test_capture_preserves_timeout_after_refused_graceful_stop(self):
        process, job = self.owned_process()
        original = subprocess.TimeoutExpired('fixture', .01)
        process.communicate.side_effect = original
        with patch.object(ownership.sys, 'platform', 'win32'), \
             patch.object(ownership, 'start_owned_subprocess', return_value=process), \
             patch.object(ownership.time, 'sleep'), \
             patch.object(ownership.time, 'monotonic', side_effect=[0, 0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                ownership.run_owned_capture(['fixture'], timeout=.01)
        self.assertIs(original, raised.exception)
        job.terminate.assert_called_once_with()
        job.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_refused_graceful_stop_does_not_hide_force_failure(self):
        process, job = self.owned_process()
        job.terminate.side_effect = OSError('force failed')
        with patch.object(ownership.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(OSError, 'force failed'):
                ownership.stop_owned_subprocess(process, timeout=0)
        process.wait.assert_not_called()

    def test_unowned_graceful_failure_remains_visible(self):
        process = Mock(_alexandria_windows_owner=None)
        with patch.object(ownership.sys, 'platform', 'win32'), \
             patch.object(ownership, 'is_subprocess_tree_running', return_value=True), \
             patch.object(ownership, 'send_subprocess_signal', side_effect=OSError('unowned refusal')), \
             patch.object(ownership, 'terminate_windows_process_tree') as force:
            with self.assertRaisesRegex(OSError, 'unowned refusal'):
                ownership.stop_owned_subprocess(process, timeout=0)
        force.assert_called_once_with(process, force=True, timeout=0)

    def test_cancel_acknowledges_refusal_only_for_live_owned_job(self):
        for active, owned in ((1, True), (0, True), (1, False)):
            with self.subTest(active=active, owned=owned):
                process, job = self.owned_process()
                job.get_active_process_count.return_value = active
                if not owned:
                    process._alexandria_windows_owner = None
                state = dict(running=True, cancel=False, process=process, pid=123)
                with patch.dict(core.process_state, {'_refusal': state}), \
                     patch.object(core.sys, 'platform', 'win32'), \
                     patch.object(core, '_send_signal_tree', side_effect=OSError('graceful refusal')):
                    if active and owned:
                        result = core._cancel_task('_refusal', 'idle', 'exited')
                        self.assertEqual({'status': 'cancel requested', 'pid': 123}, result)
                    else:
                        with self.assertRaises(core.HTTPException) as raised:
                            core._cancel_task('_refusal', 'idle', 'exited')
                        self.assertEqual(400, raised.exception.status_code)
                        self.assertEqual('exited', raised.exception.detail)
                self.assertTrue(state['cancel'])

    def test_cancel_does_not_hide_owned_job_query_failure(self):
        process, job = self.owned_process()
        job.get_active_process_count.side_effect = OSError('query failed')
        state = dict(running=True, cancel=False, process=process, pid=123)
        with patch.dict(core.process_state, {'_refusal': state}), \
             patch.object(core.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(OSError, 'query failed'):
                core._cancel_task('_refusal', 'idle', 'exited')
        self.assertTrue(state['cancel'])

    def test_refused_cancel_still_arms_force_escalation(self):
        process, job = self.owned_process()
        with patch.object(core.sys, 'platform', 'win32'), \
             patch.object(core.time, 'monotonic', side_effect=[1, 11]):
            stamp, killed = core.apply_cancel_escalation(process, None, False)
            self.assertEqual((1, False), (stamp, killed))
            stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
        self.assertTrue(killed)
        job.terminate.assert_called_once_with()


class CancellationForceFailureTests(unittest.TestCase):
    def test_closed_posix_force_channel_requires_confirmed_owner_exit(self):
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform):
                process = Mock()
                process._alexandria_control = object()
                process.wait.return_value = -9
                with patch.object(core.sys, 'platform', platform), \
                     patch.object(core.signal, 'SIGKILL', 9, create=True), \
                     patch.object(core.time, 'monotonic', return_value=20), \
                     patch.object(core, '_send_signal_tree', side_effect=BrokenPipeError('owner closed')) as force:
                    stamp, killed = core.apply_cancel_escalation(process, 0, False)
                    self.assertEqual((0, True), (stamp, killed))
                    self.assertEqual((0, True), core.apply_cancel_escalation(process, stamp, killed))
                process.wait.assert_called_once_with(timeout=core.CANCEL_TERMINATE_GRACE_SECONDS)
                force.assert_called_once_with(process, 9)

    def test_closed_force_channel_does_not_hide_live_or_unowned_targets(self):
        for platform, owned in (('win32', True), ('linux', False), ('darwin', False),
                                ('linux', True), ('darwin', True)):
            with self.subTest(platform=platform, owned=owned):
                process = Mock()
                process._alexandria_control = object() if owned else None
                failure = BrokenPipeError('owner closed')
                timeout = subprocess.TimeoutExpired('owner', core.CANCEL_TERMINATE_GRACE_SECONDS)
                process.wait.side_effect = timeout
                dispatch = 'terminate_windows_process_tree' if platform == 'win32' else '_send_signal_tree'
                expected = timeout if owned and platform != 'win32' else failure
                with patch.object(core.sys, 'platform', platform), \
                     patch.object(core.signal, 'SIGKILL', 9, create=True), \
                     patch.object(core.time, 'monotonic', return_value=20), \
                     patch.object(core, dispatch, side_effect=failure):
                    with self.assertRaises(type(expected)) as raised:
                        core.apply_cancel_escalation(process, 0, False)
                self.assertIs(expected, raised.exception)
                if expected is timeout:
                    process.wait.assert_called_once_with(timeout=core.CANCEL_TERMINATE_GRACE_SECONDS)
                else:
                    process.wait.assert_not_called()

    def test_failed_force_is_not_marked_sent_and_a_later_retry_can_succeed(self):
        for platform in ('win32', 'linux', 'darwin'):
            with self.subTest(platform=platform):
                process = Mock()
                failure = OSError('force refused')
                dispatch = 'terminate_windows_process_tree' if platform == 'win32' else '_send_signal_tree'
                with patch.object(core.sys, 'platform', platform), \
                     patch.object(core.signal, 'SIGKILL', 9, create=True), \
                     patch.object(core.time, 'monotonic', side_effect=[10, 20]), \
                     patch.object(core, dispatch, side_effect=[failure, None]) as force:
                    stamp, killed = 0, False
                    with self.assertRaises(OSError) as raised:
                        stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
                    self.assertIs(failure, raised.exception)
                    self.assertFalse(killed)
                    stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
                self.assertEqual((0, True), (stamp, killed))
                self.assertEqual(2, force.call_count)

    def test_repeated_force_errors_remain_visible_without_releasing_owner(self):
        process = Mock()
        process._alexandria_windows_owner.job.handle = 456
        with patch.object(core.sys, 'platform', 'win32'), \
             patch.object(core.time, 'monotonic', side_effect=[10, 20]), \
             patch.object(core, 'terminate_windows_process_tree', side_effect=OSError('force refused')) as force:
            stamp, killed = 0, False
            for _ in range(2):
                with self.assertRaisesRegex(OSError, 'force refused'):
                    stamp, killed = core.apply_cancel_escalation(process, stamp, killed)
                self.assertFalse(killed)
                self.assertEqual(456, process._alexandria_windows_owner.job.handle)
        self.assertEqual(2, force.call_count)
        process._alexandria_windows_owner.close.assert_not_called()

    def test_already_gone_force_target_does_not_retry_or_fail(self):
        with patch.object(core.sys, 'platform', 'win32'), \
             patch.object(core.time, 'monotonic', side_effect=[10, 20]), \
             patch.object(core, 'terminate_windows_process_tree', side_effect=ProcessLookupError) as force:
            stamp, killed = core.apply_cancel_escalation(Mock(), 0, False)
            self.assertEqual((0, True), (stamp, killed))
            self.assertEqual((stamp, killed), core.apply_cancel_escalation(Mock(), stamp, killed))
        force.assert_called_once()


class CleanupForceFailureTests(unittest.TestCase):
    def test_force_failure_propagates_before_wait_on_both_platforms(self):
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform):
                process = Mock()
                with patch.object(ownership.sys, 'platform', platform), \
                     patch.object(ownership.signal, 'SIGKILL', 9, create=True), \
                     patch.object(ownership, 'is_subprocess_tree_running', return_value=False), \
                     patch.object(ownership, 'terminate_windows_process_tree', side_effect=OSError('kernel force failed')), \
                     patch.object(ownership, 'send_subprocess_signal', side_effect=OSError('kernel force failed')):
                    with self.assertRaisesRegex(OSError, 'kernel force failed'):
                        ownership.stop_owned_subprocess(process, force_after_grace=True)
                process.wait.assert_not_called()

    def test_closed_posix_owner_channel_requires_bounded_exit(self):
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform):
                process = Mock()
                process._alexandria_control = object()
                with patch.object(ownership.sys, 'platform', platform), \
                     patch.object(ownership.signal, 'SIGKILL', 9, create=True), \
                     patch.object(ownership, 'is_subprocess_tree_running', return_value=False), \
                     patch.object(ownership, 'send_subprocess_signal', side_effect=BrokenPipeError('owner closed')):
                    ownership.stop_owned_subprocess(process, timeout=.2, force_after_grace=True)
                process.wait.assert_called_once_with(timeout=.2)

    def test_closed_owner_channel_does_not_hide_unconfirmed_cleanup(self):
        for case in ('live_posix_owner', 'unowned_posix', 'windows_owner'):
            with self.subTest(case=case):
                process = Mock()
                process._alexandria_control = None if case == 'unowned_posix' else object()
                process.wait.side_effect = subprocess.TimeoutExpired('owned supervisor', .2)
                platform = 'win32' if case == 'windows_owner' else 'linux'
                expected = subprocess.TimeoutExpired if case == 'live_posix_owner' else BrokenPipeError
                with patch.object(ownership.sys, 'platform', platform), \
                     patch.object(ownership.signal, 'SIGKILL', 9, create=True), \
                     patch.object(ownership, 'is_subprocess_tree_running', return_value=False), \
                     patch.object(ownership, 'terminate_windows_process_tree', side_effect=BrokenPipeError('force fault')), \
                     patch.object(ownership, 'send_subprocess_signal', side_effect=BrokenPipeError('owner closed')):
                    with self.assertRaises(expected):
                        ownership.stop_owned_subprocess(process, timeout=.2, force_after_grace=True)
                if case == 'live_posix_owner':
                    process.wait.assert_called_once_with(timeout=.2)
                else:
                    process.wait.assert_not_called()

    def test_reaped_owner_does_not_replace_original_shutdown_error(self):
        process = Mock()
        process._alexandria_control = object()
        with patch.object(ownership.sys, 'platform', 'linux'), \
             patch.object(ownership.signal, 'SIGKILL', 9, create=True), \
             patch.object(ownership, 'is_subprocess_tree_running', side_effect=RuntimeError('inspection failed')), \
             patch.object(ownership, 'send_subprocess_signal', side_effect=BrokenPipeError('owner closed')):
            with self.assertRaisesRegex(RuntimeError, 'inspection failed'):
                ownership.stop_owned_subprocess(process, timeout=.2, force_after_grace=True)
        process.wait.assert_called_once_with(timeout=.2)

    def test_verifier_retains_posix_policy_and_escalates_only_on_windows(self):
        for platform in ('win32', 'linux', 'darwin'):
            with self.subTest(platform=platform):
                process = Mock()
                with patch.object(verifier.sys, 'platform', platform), \
                     patch.object(verifier, 'stop_owned_subprocess') as stop:
                    verifier.stop_process_group(process, timeout=2)
                stop.assert_called_once_with(process, interrupt=False, timeout=2,
                                             force_after_grace=platform == 'win32')

    def test_native_runner_is_explicitly_wired_and_refuses_other_hosts(self):
        root = Path(__file__).resolve().parents[2]
        script = root / 'app/tests/native_windows_cleanup_checks.py'
        workflow = (root / '.github/workflows/tests.yml').read_text(encoding='utf-8')
        self.assertIn('run: python tests/native_windows_cleanup_checks.py', workflow)
        requirements = (root / 'app/requirements.txt').read_text(encoding='utf-8').splitlines()
        requests_pin = next(line for line in requirements if line.startswith('requests=='))
        windows_setup = workflow.split('  windows-owner:', 1)[1].split(
            '      - name: Verify native Windows qualification cleanup', 1)[0]
        self.assertIn(requests_pin, windows_setup)
        probe = "import runpy,sys; sys.platform='darwin'; runpy.run_path(sys.argv[1],run_name='__main__')"
        result = subprocess.run([sys.executable, '-B', '-c', probe, str(script)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('nothing was verified', result.stderr)
        self.assertNotIn('OK', result.stderr)


if __name__ == '__main__':
    unittest.main()
