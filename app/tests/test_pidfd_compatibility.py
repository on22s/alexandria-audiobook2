"""Python pidfds work without glibc wrappers and fail closed on missing support."""
import errno
import os
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import subprocess_ownership as ownership


class PidfdCompatibilityTests(unittest.TestCase):
    def test_missing_python_operation_refuses_admission(self):
        for module, name in ((os, "pidfd_open"), (signal, "pidfd_send_signal")):
            with self.subTest(name=name), patch.object(module, name, None, create=True), \
                 patch.object(ownership.ctypes, "CDLL", return_value=object()):
                with self.assertRaises(OSError) as caught:
                    ownership.ensure_pidfd_support()
                self.assertEqual(errno.ENOSYS, caught.exception.errno)

    def test_probe_checks_signal_zero_and_closes_descriptor(self):
        with patch.object(os, "pidfd_open", return_value=42, create=True) as opened, \
             patch.object(signal, "pidfd_send_signal", create=True) as sent, \
             patch.object(os, "close") as closed, \
             patch.object(ownership.ctypes, "CDLL", side_effect=AssertionError("glibc wrapper used")):
            ownership.ensure_pidfd_support()
        opened.assert_called_once_with(os.getpid(), 0)
        sent.assert_called_once_with(42, 0, None, 0)
        closed.assert_called_once_with(42)

    def test_kernel_open_failure_preserves_errno_without_sending(self):
        with patch.object(os, "pidfd_open", side_effect=OSError(errno.ENOSYS, "unsupported"), create=True), \
             patch.object(signal, "pidfd_send_signal", create=True) as sent, \
             patch.object(os, "close") as closed:
            with self.assertRaises(OSError) as caught:
                ownership.ensure_pidfd_support()
        self.assertEqual(errno.ENOSYS, caught.exception.errno)
        sent.assert_not_called()
        closed.assert_not_called()

    def test_signal_probe_failure_preserves_errno_and_closes_descriptor(self):
        with patch.object(os, "pidfd_open", return_value=42, create=True), \
             patch.object(signal, "pidfd_send_signal", side_effect=PermissionError(errno.EPERM, "denied"), create=True), \
             patch.object(os, "close") as closed:
            with self.assertRaises(OSError) as caught:
                ownership.ensure_pidfd_support()
        self.assertEqual(errno.EPERM, caught.exception.errno)
        closed.assert_called_once_with(42)

    def test_legacy_python_uses_libc_handles_with_same_probe(self):
        libc = SimpleNamespace(pidfd_open=Mock(return_value=42),
                               pidfd_send_signal=Mock(return_value=0))
        with patch.object(os, "pidfd_open", None, create=True), \
             patch.object(ownership.ctypes, "CDLL", return_value=libc) as loaded, \
             patch.object(os, "close") as closed:
            pidfds = ownership.ensure_pidfd_support()
            self.assertEqual(42, ownership.open_owned_process(pidfds, 123))
            pidfds.pidfd_send_signal(42, signal.SIGTERM, None, 0)
        loaded.assert_called_once_with(None, use_errno=True)
        self.assertEqual([(os.getpid(), 0), (123, 0)],
                         [call.args for call in libc.pidfd_open.call_args_list])
        self.assertEqual([(42, 0, None, 0), (42, signal.SIGTERM, None, 0)],
                         [call.args for call in libc.pidfd_send_signal.call_args_list])
        closed.assert_called_once_with(42)

    def test_legacy_probe_preserves_errno_and_closes_on_signal_failure(self):
        libc = SimpleNamespace(pidfd_open=Mock(return_value=42),
                               pidfd_send_signal=Mock(return_value=-1))
        with patch.object(os, "pidfd_open", None, create=True), \
             patch.object(ownership.ctypes, "CDLL", return_value=libc), \
             patch.object(ownership.ctypes, "get_errno", return_value=errno.EPERM), \
             patch.object(os, "close") as closed:
            with self.assertRaises(OSError) as caught:
                ownership.ensure_pidfd_support()
        self.assertEqual(errno.EPERM, caught.exception.errno)
        closed.assert_called_once_with(42)

    def run_signal(self, *, identity="birth", error=None):
        delivered = set()
        with patch.object(ownership, "get_owned_descendants", return_value={123: "birth"}), \
             patch.object(ownership, "get_process_identity", return_value=identity), \
             patch.object(os, "pidfd_open", return_value=42, create=True), \
             patch.object(signal, "pidfd_send_signal", side_effect=error, create=True) as sent, \
             patch.object(os, "kill", side_effect=AssertionError("numeric PID signal used")), \
             patch.object(os, "close") as closed:
            ownership.apply_descendant_signal(SimpleNamespace(pidfd_open=os.pidfd_open, pidfd_send_signal=signal.pidfd_send_signal), signal.SIGTERM, delivered)
        closed.assert_called_once_with(42)
        return delivered, sent

    def test_signal_uses_handle_and_records_verified_identity(self):
        delivered, sent = self.run_signal()
        sent.assert_called_once_with(42, signal.SIGTERM, None, 0)
        self.assertEqual({(123, "birth", signal.SIGTERM)}, delivered)

    def test_recycled_pid_is_not_signalled(self):
        delivered, sent = self.run_signal(identity="replacement")
        sent.assert_not_called()
        self.assertEqual(set(), delivered)

    def test_departed_process_is_not_recorded_as_signalled(self):
        delivered, _ = self.run_signal(error=ProcessLookupError(errno.ESRCH, "gone"))
        self.assertEqual(set(), delivered)

    def test_permission_failure_propagates_and_closes_handle(self):
        with patch.object(ownership, "get_owned_descendants", return_value={123: "birth"}), \
             patch.object(ownership, "get_process_identity", return_value="birth"), \
             patch.object(os, "pidfd_open", return_value=42, create=True), \
             patch.object(signal, "pidfd_send_signal", side_effect=PermissionError(errno.EPERM, "denied"), create=True), \
             patch.object(os, "close") as closed:
            delivered = set()
            with self.assertRaises(PermissionError):
                ownership.apply_descendant_signal(SimpleNamespace(pidfd_open=os.pidfd_open, pidfd_send_signal=signal.pidfd_send_signal), signal.SIGTERM, delivered)
        closed.assert_called_once_with(42)
        self.assertEqual(set(), delivered)


@unittest.skipUnless(sys.platform == "linux", "Native Linux pidfd probe")
class NativePidfdCompatibilityTests(unittest.TestCase):
    def test_real_probe_uses_available_pidfd_implementation(self):
        ownership.ensure_pidfd_support()
