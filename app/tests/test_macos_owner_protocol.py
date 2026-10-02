"""Real Unix transport and worker protocol; mocked kernel is not Darwin proof."""
import array
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import macos_subprocess_owner as owner


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


class MacOwnerCleanupTests(unittest.TestCase):
    def test_unknown_reap_query_and_remove_timeout_retain_cleanup_until_esrch(self):
        api = Mock()
        api.get_active_count.side_effect = [OSError(errno.EPERM, 'unknown'), 0, 1, OSError(errno.ESRCH, 'reaped')]
        api.get_coalition_members.return_value = []
        with patch.object(owner.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('remove', 30), Mock(returncode=0)]) as remove, patch.object(owner.time, 'sleep') as sleep, patch('sys.stderr'):
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
