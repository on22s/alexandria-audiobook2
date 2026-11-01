"""Real Unix transport and worker protocol; mocked kernel is not Darwin proof."""
import array
import errno
try:
    import fcntl
except ImportError:
    fcntl = None
HAVE_FCNTL = globals().get('fcntl') is not None
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if HAVE_FCNTL:
    import macos_subprocess_owner as owner
else:
    owner = None


@unittest.skipIf(not HAVE_FCNTL, "POSIX kernel locks (fcntl) are unavailable on this host")
class MacOwnerTransportTests(unittest.TestCase):
    def test_transferred_descriptor_retains_real_lock_after_sender_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lease'
            with path.open('w+b') as original:
                left, right = socket.socketpair()
                with left, right:
                    fcntl.flock(original, fcntl.LOCK_EX)
                    owner.send_macos_owner_descriptor(left, original.fileno())
                    received = owner.receive_macos_owner_descriptor(right)
                try:
                    self.assertFalse(os.get_inheritable(received))
                    original.close()
                    with path.open('r+b') as contender:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(received)
                with path.open('r+b') as contender:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_bad_descriptor_packet_closes_received_lock(self):
        for marker, count in ((b'X', 1), (b'F', 2), (b'F', 3)):
            with self.subTest(marker=marker, count=count), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'lease'
                with path.open('w+b') as source:
                    fcntl.flock(source, fcntl.LOCK_EX)
                    left, right = socket.socketpair()
                    with left, right:
                        left.sendmsg([marker], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                array.array('i', [source.fileno()] * count))])
                        with self.assertRaises(ValueError):
                            owner.receive_macos_owner_descriptor(right)
                    source.close()
                    with path.open('r+b') as contender:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_incomplete_configuration_is_error(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(b'partial')
            left.shutdown(socket.SHUT_WR)
            with self.assertRaises(OSError):
                owner.receive_macos_owner_bytes(right, 20)

    def test_real_worker_pass_fd_can_reuse_original_control_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            address = str(Path(tmp) / 'control')
            with socket.socket(socket.AF_UNIX) as server, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as extra, open(os.devnull, 'rb') as null:
                server.bind(address)
                server.listen(1)
                code = "import macos_subprocess_owner as m; from unittest.mock import Mock; m.MacApi=lambda: Mock(get_info=lambda p: {'pid':p,'coalition':42},get_active_count=lambda c:1); m.run_macos_task_job(__import__('sys').argv[1])"
                # With close_fds, the job's first socket is fd 3: the explicit worker target.
                process = subprocess.Popen([sys.executable, '-c', code, address], stderr=subprocess.PIPE, close_fds=True)
                try:
                    actor, _ = server.accept()
                    with actor:
                        actor.settimeout(5)
                        greeting = owner.receive_macos_owner_line(actor)
                        self.assertEqual(process.pid, greeting['owner']['pid'])
                        config = {'command': [sys.executable, '-c', "import os; os.write(3,b'capability'); print(os.environ['OWNER_TEST']); print(os.getcwd())"], 'worker': {'pass_fds': [3]}, 'cwd': tmp, 'env': {**os.environ, 'OWNER_TEST': 'passed'}}
                        payload = json.dumps(config).encode()
                        actor.sendall(len(payload).to_bytes(8, 'big') + payload)
                        for fd in (null.fileno(), output.fileno(), output.fileno(), extra.fileno()):
                            owner.send_macos_owner_descriptor(actor, fd)
                        self.assertIn('started', owner.receive_macos_owner_line(actor))
                        self.assertEqual({'root_exit': 0}, owner.receive_macos_owner_line(actor))
                        actor.sendall(b'fin')
                        actor.sendall(b'ish\n')
                    self.assertEqual(0, process.wait(timeout=5), process.stderr.read().decode())
                    output.seek(0)
                    self.assertEqual(f'passed\n{tmp}\n'.encode(), output.read())
                    extra.seek(0)
                    self.assertEqual(b'capability', extra.read())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    process.stderr.close()


@unittest.skipIf(not HAVE_FCNTL, "POSIX kernel locks (fcntl) are unavailable on this host")
class MacOwnerCleanupTests(unittest.TestCase):
    def test_unavailable_launchctl_executable_does_not_wait_for_job_cleanup(self):
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        control, peer = socket.socketpair()
        api = Mock()
        api.get_info.return_value = {'pid': 1, 'coalition': 1}
        spec = {'stop_receipt': None}
        try:
            with control, peer, patch.object(owner, 'MacApi', return_value=api), \
                    patch.object(owner.subprocess, 'run', side_effect=FileNotFoundError('missing executable')), \
                    patch.object(owner, 'ensure_macos_unadmitted_job_removed',
                                 side_effect=AssertionError('impossible submission was retried')):
                with self.assertRaises(FileNotFoundError):
                    owner.run_macos_owner_broker(control, spec)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    def test_failed_admission_does_not_leave_a_keepalive_job(self):
        with patch.object(owner, 'ensure_macos_job_removed', side_effect=[False, False, True]) as remove, \
                patch.object(owner.time, 'sleep') as sleep, patch('sys.stderr'):
            owner.ensure_macos_unadmitted_job_removed('fixture-job')
        self.assertEqual(3, remove.call_count)
        self.assertEqual(2, sleep.call_count)

    def test_job_absence_requires_complete_successful_listing(self):
        label = 'org.alexandria.task.fixture'
        header = b'PID\tStatus\tLabel\n'
        cases = ((header + b'42\t-\tother\n-\t-\tidle\n43\t???\tunknown\n', True),
                 (header + b'-\t0\t' + label.encode() + b'\n', False),
                 (header + b'-\t0\t' + label.encode()[:-1], False),
                 (header + b'42\t--1\tother\n', False),
                 (header + '١\t0\tother\n'.encode(), False),
                 (header + b'broken row\n', False),
                 (b'', False), (None, False), (header + b'\xff\n', False))
        for data, expected in cases:
            with self.subTest(data=data), patch.object(owner, '_bounded_command_output', return_value=data):
                self.assertEqual(expected, owner._macos_job_is_absent(label))

    def test_listing_capture_enforces_output_and_time_limits(self):
        normal = owner._bounded_command_output([sys.executable, '-c', "print('small')"], limit=32, timeout=3)
        self.assertEqual(b'small\n', normal)
        excess = owner._bounded_command_output([sys.executable, '-c', "import sys; sys.stdout.write('x'*1000000)"],
                                               limit=128, timeout=3)
        self.assertIsNone(excess)
        timeout = owner._bounded_command_output([sys.executable, '-c', 'import time; time.sleep(10)'],
                                                limit=128, timeout=0.2)
        self.assertIsNone(timeout)
        closed_pipe = owner._bounded_command_output(
            [sys.executable, '-c', 'import os,time; os.close(1); time.sleep(10)'], limit=128, timeout=0.2)
        self.assertIsNone(closed_pipe)

    def test_timed_out_removal_can_be_resolved_by_verified_absence(self):
        with patch.object(owner.subprocess, 'run', side_effect=subprocess.TimeoutExpired('remove', 30)), \
                patch.object(owner, '_bounded_command_output', return_value=b'PID\tStatus\tLabel\n-\t0\tother\n'):
            self.assertTrue(owner.ensure_macos_job_removed('org.alexandria.task.fixture'))

    def test_reaped_cleanup_still_requires_successful_job_removal(self):
        api = Mock()
        api.get_active_count.side_effect = [OSError(errno.ESRCH, 'reaped')] * 3
        errors = io.StringIO()
        fake = SimpleNamespace(run=Mock(side_effect=[Mock(returncode=1), Mock(returncode=1),
                                                     Mock(returncode=0)]),
                               TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=subprocess.DEVNULL)
        with patch.object(owner, 'subprocess', fake), patch.object(owner.time, 'sleep') as sleep, \
                patch.object(owner, '_macos_job_is_absent', return_value=False), patch('sys.stderr', errors):
            owner.ensure_macos_scope_reaped(api, 42, 'fixture-job')
        self.assertEqual(3, fake.run.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertIn('removal failed; retaining ownership', errors.getvalue())
        self.assertEqual(1, errors.getvalue().count('removal failed'))

    def test_cleanup_does_not_remove_a_previously_removed_job_again(self):
        api = Mock()
        api.get_active_count.side_effect = OSError(errno.ESRCH, 'reaped')
        with patch.object(owner, 'ensure_macos_job_removed', side_effect=AssertionError('already removed')):
            owner.ensure_macos_scope_reaped(api, 42, 'fixture-job', removal_confirmed=True)
    def test_unknown_reap_query_and_remove_timeout_retain_cleanup_until_esrch(self):
        api = Mock()
        api.get_active_count.side_effect = [OSError(errno.EPERM, 'unknown'), 0, 1, OSError(errno.ESRCH, 'reaped')]
        api.get_coalition_members.return_value = []
        with patch.object(owner.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('remove', 30), Mock(returncode=0)]) as remove, patch.object(owner, '_macos_job_is_absent', return_value=False), patch.object(owner.time, 'sleep') as sleep, patch('sys.stderr'):
            owner.ensure_macos_scope_reaped(api, 42, 'fixture-job')
        self.assertEqual(4, api.get_active_count.call_count)
        self.assertEqual(2, remove.call_count)
        self.assertEqual(3, sleep.call_count)

    def test_signal_refusal_is_not_success_or_empty_membership(self):
        api = Mock()
        member = {'pid': 4, 'pidversion': 9, 'coalition': 42}
        api.get_coalition_members.return_value = [member]
        api.apply_signal.return_value = errno.EPERM
        with self.assertRaises(OSError) as raised:
            owner.apply_macos_scope_signal(api, 42, signal.SIGTERM)
        self.assertEqual(errno.EPERM, raised.exception.errno)
        api.apply_signal.assert_called_once_with(member, signal.SIGTERM)

    def test_vanished_exact_identity_does_not_signal_recycled_pid(self):
        api = Mock()
        api.get_coalition_members.return_value = [{'pid': 4, 'pidversion': 9, 'coalition': 42}]
        api.apply_signal.return_value = errno.ESRCH
        owner.apply_macos_scope_signal(api, 42, signal.SIGKILL)
        self.assertEqual(1, api.apply_signal.call_count)


class FakeLaunchdScope:
    """One private coalition under a KeepAlive `launchctl submit` job.

    Models what the #1646 native samples showed: when the actor exits, launchd
    starts a replacement (parent launchd, launched after the worker finished,
    blocked receiving a configuration) unless the job was removed first. The
    actor itself is a real thread speaking the real socket protocol.
    `actor_mode` 'finish' completes the root and waits for `finish`; 'drop'
    closes the socket before any root exit, as a killed actor would.
    """
    BROKER = {'pid': 100, 'pidversion': 1, 'coalition': 1}
    ACTOR = {'pid': 200, 'pidversion': 1, 'coalition': 7}

    def __init__(self, *, keep_alive=True, reap_query_error=None, failed_removes=0, actor_mode='finish',
                 reap_before_removal=False):
        self.lock = threading.Lock()
        self.keep_alive = keep_alive
        self.reap_query_error = reap_query_error
        self.failed_removes = failed_removes
        self.actor_mode = actor_mode
        self.reap_before_removal = reap_before_removal
        self.actor_alive = False
        self.replacements = 0
        self.restarted = 0
        self.finish_received = False
        self.removed = False
        self.removed_after_release = False
        self.remove_calls = 0
        self.released = False
        self.thread = None
        self.actor_error = None
        self.actor_exited = threading.Event()

    # MacApi surface used by the broker.
    def get_info(self, pid):
        return dict(self.BROKER if pid == os.getpid() else self.ACTOR)

    def get_active_count(self, coalition):
        with self.lock:
            if self.released:
                raise OSError(errno.ESRCH, 'watchdog released the fixture')
            if self.reap_query_error is not None and not self.actor_alive and self.removed:
                raise OSError(self.reap_query_error, 'unobservable scope')
            count = int(self.actor_alive) + self.replacements
            if count == 0 and (self.removed or self.reap_before_removal):
                raise OSError(errno.ESRCH, 'reaped')
            return count

    def get_coalition_members(self, coalition):
        return []

    def apply_signal(self, member, sig):
        return errno.ESRCH

    def run(self, argv, **kwargs):
        if argv[1] == 'submit':
            with self.lock:
                self.actor_alive = True
            self.thread = threading.Thread(target=self.run_actor, args=(argv[-1],), daemon=True)
            self.thread.start()
        elif argv[1] == 'remove':
            with self.lock:
                self.remove_calls += 1
                failed = self.remove_calls <= self.failed_removes and not self.released
                if not failed:
                    self.removed = True
                    self.removed_after_release = self.released
                    self.replacements = 0
            if failed:
                if self.reap_before_removal:
                    self.actor_exited.wait(2)
                return Mock(returncode=1, stderr='')
        return Mock(returncode=0, stderr='')

    def run_actor(self, address):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(10)
                channel.connect(address)
                owner.send_macos_owner_event(channel, {'owner': self.ACTOR})
                size = int.from_bytes(owner.receive_macos_owner_bytes(channel, 8), 'big')
                json.loads(owner.receive_macos_owner_bytes(channel, size))
                for _ in range(3):
                    os.close(owner.receive_macos_owner_descriptor(channel))
                owner.send_macos_owner_event(channel, {'started': 300})
                if self.actor_mode == 'drop':
                    return
                owner.send_macos_owner_event(channel, {'root_exit': 0})
                data = b''
                while b'\n' not in data:
                    part = channel.recv(64)
                    if not part:
                        break
                    data += part
                self.finish_received = data == b'finish\n'
        except BaseException as error:
            self.actor_error = error
        finally:
            with self.lock:
                self.actor_alive = False
                if self.keep_alive and not self.removed:
                    self.replacements += 1
                    self.restarted += 1
            self.actor_exited.set()


@unittest.skipIf(not HAVE_FCNTL, "POSIX kernel locks (fcntl) are unavailable on this host")
class MacOwnerFinishTests(unittest.TestCase):
    """The broker's finish and stop paths against a KeepAlive launchd job (#1646)."""

    def run_broker(self, scope, *, deadline=10):
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        timed_out = threading.Event()

        def release():
            timed_out.set()
            with scope.lock:
                scope.released = True

        watchdog = threading.Timer(deadline, release)
        control, peer = socket.socketpair()
        # Scoped to the owner module: the shared subprocess module stays real.
        fake_subprocess = SimpleNamespace(run=scope.run, TimeoutExpired=subprocess.TimeoutExpired,
                                          DEVNULL=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'stop.json'
            spec = {'command': ['worker'], 'worker': {'pass_fds': []}, 'stop_receipt': str(receipt),
                    'exit_notice': None, 'disconnect_signal': int(signal.SIGKILL), 'grace': 5}
            try:
                with control, peer, patch.object(owner, 'MacApi', return_value=scope), \
                        patch.object(owner, 'subprocess', fake_subprocess), \
                        patch.object(owner, '_macos_job_is_absent', return_value=False), patch('sys.stderr'):
                    watchdog.start()
                    result = owner.run_macos_owner_broker(control, spec)
                    self.assertEqual({'started': 300}, owner.receive_macos_owner_line(peer))
            finally:
                watchdog.cancel()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                if scope.thread is not None:
                    scope.thread.join(5)
            self.assertFalse(scope.thread.is_alive())
            self.assertIsNone(scope.actor_error)
            return result, timed_out.is_set(), receipt.exists()

    def test_fixture_restarts_an_actor_that_exits_before_removal(self):
        # The fixture must discriminate: without removal, finishing the actor
        # leaves a replacement member, so "wait for zero" can never end.
        scope = FakeLaunchdScope(keep_alive=True)
        scope.removed = False
        scope.actor_alive = True
        scope.run_actor('/nonexistent/address')
        self.assertIsInstance(scope.actor_error, OSError)
        self.assertEqual((1, 1), (scope.restarted, scope.get_active_count(7)))

    def test_restarted_actor_does_not_hold_completed_scope_open(self):
        scope = FakeLaunchdScope(keep_alive=True)
        result, timed_out, receipt = self.run_broker(scope)
        self.assertFalse(timed_out, 'broker waited on a launchd-restarted actor')
        self.assertTrue(scope.finish_received)
        self.assertEqual((0, True, 1), (result, receipt, scope.remove_calls))
        self.assertEqual(0, scope.replacements)

    def test_failed_remove_is_retried_until_launchd_accepts_it(self):
        scope = FakeLaunchdScope(keep_alive=True, failed_removes=3)
        result, timed_out, receipt = self.run_broker(scope)
        self.assertFalse(timed_out)
        self.assertEqual((0, True, 4), (result, receipt, scope.remove_calls))

    def test_reaped_scope_does_not_release_until_job_removal_succeeds(self):
        # The old coalition may disappear while a failed KeepAlive removal is
        # still pending. ESRCH alone must not abandon the launchd job.
        scope = FakeLaunchdScope(keep_alive=False, failed_removes=1, reap_before_removal=True)
        result, timed_out, receipt = self.run_broker(scope)
        self.assertFalse(timed_out)
        self.assertEqual((0, True, 2), (result, receipt, scope.remove_calls))
        self.assertTrue(scope.removed)

    def test_persistent_remove_failures_are_rate_limited_and_keep_ownership(self):
        scope = FakeLaunchdScope(keep_alive=True, failed_removes=100)
        _, timed_out, _ = self.run_broker(scope, deadline=1.5)
        self.assertTrue(timed_out)
        self.assertLessEqual(scope.remove_calls, 5)
        self.assertTrue(scope.removed_after_release)

    def test_killed_actor_restart_does_not_block_stop_cleanup(self):
        # Actor lost before the root exited: the broker kills the scope, and a
        # launchd replacement must not keep that stop waiting.
        scope = FakeLaunchdScope(keep_alive=True, actor_mode='drop')
        result, timed_out, receipt = self.run_broker(scope)
        self.assertFalse(timed_out)
        self.assertEqual((-signal.SIGKILL, True), (result, receipt))
        self.assertEqual(0, scope.replacements)

    def test_without_restart_completion_is_unchanged(self):
        scope = FakeLaunchdScope(keep_alive=False)
        result, timed_out, receipt = self.run_broker(scope)
        self.assertFalse(timed_out)
        self.assertEqual((0, True, 0), (result, receipt, scope.restarted))

    def test_unobservable_scope_after_finish_keeps_ownership(self):
        # Removal is not reaping: an unknown membership answer must keep the
        # broker (and its leases) waiting until the watchdog's ESRCH ends it.
        scope = FakeLaunchdScope(keep_alive=True, reap_query_error=errno.EPERM)
        _, timed_out, _ = self.run_broker(scope, deadline=1.5)
        self.assertTrue(timed_out)
        self.assertTrue(scope.finish_received)
        self.assertEqual(1, scope.remove_calls)


@unittest.skipIf(not HAVE_FCNTL, "POSIX kernel locks (fcntl) are unavailable on this host")
class MacOwnerAdmissionTests(unittest.TestCase):
    def test_shared_scope_and_changed_identity_are_rejected(self):
        candidate = {'pid': 5, 'pidversion': 8, 'coalition': 42}
        for observed, broker_scope in ((candidate, 42), ({**candidate, 'pidversion': 9}, 99)):
            with self.subTest(observed=observed, broker_scope=broker_scope):
                api = Mock()
                api.get_info.return_value = observed
                with self.assertRaises(OSError):
                    owner.get_verified_macos_task_scope(api, candidate, {'coalition': broker_scope})
                api.get_active_count.assert_not_called()
                api.apply_signal.assert_not_called()

    def test_nonexclusive_and_unknown_scope_are_rejected(self):
        candidate = {'pid': 5, 'pidversion': 8, 'coalition': 42}
        for count in (0, 2, OSError(errno.EPERM, 'unknown')):
            with self.subTest(count=count):
                api = Mock()
                api.get_info.return_value = candidate
                if isinstance(count, OSError):
                    api.get_active_count.side_effect = count
                else:
                    api.get_active_count.return_value = count
                with self.assertRaises(OSError):
                    owner.get_verified_macos_task_scope(api, candidate, {'coalition': 99})
                api.apply_signal.assert_not_called()
