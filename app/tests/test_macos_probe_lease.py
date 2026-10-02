"""Native Unix descriptor transport for the Mac probe; not Apple kernel proof."""
import array
import errno
import fcntl
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[2] / 'tools/diagnostics/macos_369_native_probe.py'
spec = importlib.util.spec_from_file_location('macos_probe_lease_fixture', SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class MacProbeLeaseTests(unittest.TestCase):
    def test_real_descriptor_keeps_lease_after_sender_closes_then_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lease'
            with path.open('w+b') as source:
                # A paired channel is used below; no Apple API is invoked on Linux.
                left, right = socket.socketpair()
                with left, right:
                    fcntl.flock(source, fcntl.LOCK_EX)
                    left.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                         array.array('i', [source.fileno()]))])
                    held = probe.receive_probe_lease(right, path)
                try:
                    source.close()
                    self.assertFalse(probe.is_probe_lease_free(path))
                finally:
                    os.close(held)
                self.assertTrue(probe.is_probe_lease_free(path))

    def test_wrong_file_descriptor_is_rejected_and_received_copy_closed(self):
        self.check_rejected('wrong-file')

    def test_multiple_lease_descriptors_are_rejected_without_leaking_lock(self):
        self.check_rejected('multiple')

    def test_wrong_admission_message_is_rejected_without_leaking_lock(self):
        self.check_rejected('message')

    def check_rejected(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lease'
            path.touch()
            other = Path(tmp) / 'other'
            with (other if mode == 'wrong-file' else path).open('w+b') as source:
                fcntl.flock(source, fcntl.LOCK_EX)
                left, right = socket.socketpair()
                with left, right:
                    fds = [source.fileno()] * (2 if mode == 'multiple' else 1)
                    left.sendmsg([b'X' if mode == 'message' else b'L'],
                                 [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', fds))])
                    with self.assertRaises(ValueError):
                        probe.receive_probe_lease(right, path)
                source.close()
                self.assertTrue(probe.is_probe_lease_free(other if mode == 'wrong-file' else path))


class MacProbeReapTests(unittest.TestCase):
    def test_zero_resource_tasks_does_not_certify_reaped_admission_scope(self):
        api = Mock()
        api.get_active_count.side_effect = [0, OSError(errno.ESRCH, 'reaped')]
        self.assertFalse(probe.is_probe_coalition_reaped(api, 4831))
        self.assertTrue(probe.is_probe_coalition_reaped(api, 4831))

    def test_unknown_kernel_error_does_not_become_an_empty_scope(self):
        for error in (errno.EPERM, errno.EIO, errno.EINVAL):
            with self.subTest(error=error):
                api = Mock()
                api.get_active_count.side_effect = OSError(error, 'fixture refusal')
                with self.assertRaises(OSError) as raised:
                    probe.is_probe_coalition_reaped(api, 4831)
                self.assertEqual(error, raised.exception.errno)
