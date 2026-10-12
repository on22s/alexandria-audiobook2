"""Python pidfds work without glibc wrappers and fail closed on missing support."""
import errno
import ctypes
import os
import select
import signal
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import subprocess_ownership as ownership


def clear_pidfd_cache(test):
    """ensure_pidfd_support is cached per process; every test selects afresh."""
    ownership.ensure_pidfd_support.cache_clear()
    test.addCleanup(ownership.ensure_pidfd_support.cache_clear)


class PidfdCompatibilityTests(unittest.TestCase):
    def setUp(self):
        clear_pidfd_cache(self)

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
            self.assertEqual("libc", pidfds.implementation)
            self.assertEqual(42, pidfds.pidfd_open(123, 0))
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

    def production_pidfds(self, opened, sent):
        """The dispatch ensure_pidfd_support returns over the given Python APIs."""
        with patch.object(os, "pidfd_open", opened, create=True), \
             patch.object(signal, "pidfd_send_signal", sent, create=True), \
             patch.object(os, "close"):
            pidfds = ownership.ensure_pidfd_support()
        self.assertEqual("python", pidfds.implementation)
        opened.reset_mock()
        sent.reset_mock()
        return pidfds

    def run_signal(self, *, identity="birth", error=None):
        delivered = set()
        sent = Mock()
        pidfds = self.production_pidfds(Mock(return_value=42), sent)
        sent.side_effect = error
        with patch.object(ownership, "get_owned_descendants", return_value={123: "birth"}), \
             patch.object(ownership, "get_process_identity", return_value=identity), \
             patch.object(os, "kill", side_effect=AssertionError("numeric PID signal used")), \
             patch.object(os, "close") as closed:
            ownership.apply_descendant_signal(pidfds, signal.SIGTERM, delivered)
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
        sent = Mock()
        pidfds = self.production_pidfds(Mock(return_value=42), sent)
        sent.side_effect = PermissionError(errno.EPERM, "denied")
        with patch.object(ownership, "get_owned_descendants", return_value={123: "birth"}), \
             patch.object(ownership, "get_process_identity", return_value="birth"), \
             patch.object(os, "close") as closed:
            delivered = set()
            with self.assertRaises(PermissionError):
                ownership.apply_descendant_signal(pidfds, signal.SIGTERM, delivered)
        closed.assert_called_once_with(42)
        self.assertEqual(set(), delivered)


    def test_probe_error_names_the_failing_step(self):
        with patch.object(os, "pidfd_open", return_value=42, create=True), \
             patch.object(signal, "pidfd_send_signal", side_effect=PermissionError(errno.EPERM, "denied"),
                          create=True), \
             patch.object(os, "close"):
            with self.assertRaises(OSError) as caught:
                ownership.ensure_pidfd_support()
        self.assertIn("pidfd_send_signal probe failed", str(caught.exception))

    def test_selection_and_probe_run_once_per_process(self):
        opened = Mock(return_value=42)
        with patch.object(os, "pidfd_open", opened, create=True), \
             patch.object(signal, "pidfd_send_signal", create=True), patch.object(os, "close"):
            first = ownership.ensure_pidfd_support()
            self.assertIs(first, ownership.ensure_pidfd_support())
        opened.assert_called_once_with(os.getpid(), 0)


def libc_has_pidfd():
    libc = ctypes.CDLL(None)
    return hasattr(libc, "pidfd_open") and hasattr(libc, "pidfd_send_signal")


def spawn_sleeper(test):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    test.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
    return child


@unittest.skipUnless(sys.platform == "linux", "Native Linux pidfd probe")
class NativePidfdCompatibilityTests(unittest.TestCase):
    def setUp(self):
        clear_pidfd_cache(self)

    def terminate_through(self, pidfds):
        child = spawn_sleeper(self)
        descriptor = pidfds.pidfd_open(child.pid, 0)
        try:
            pidfds.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
            self.assertTrue(select.select([descriptor], [], [], 10)[0], "child did not exit")
        finally:
            os.close(descriptor)
        self.assertEqual(-signal.SIGTERM, child.wait(10))

    def test_real_probe_picks_the_available_implementation_and_signals_a_child(self):
        pidfds = ownership.ensure_pidfd_support()
        expected = "python" if callable(getattr(os, "pidfd_open", None)) and \
            callable(getattr(signal, "pidfd_send_signal", None)) else "libc"
        self.assertEqual(expected, pidfds.implementation)
        self.terminate_through(pidfds)


@unittest.skipUnless(sys.platform == "linux" and libc_has_pidfd(), "glibc pidfd wrappers")
class LibcFallbackTests(unittest.TestCase):
    """The libc branch, forced on any interpreter, against real processes."""

    def setUp(self):
        clear_pidfd_cache(self)
        for module, name in ((os, "pidfd_open"), (signal, "pidfd_send_signal")):
            patcher = patch.object(module, name, None, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pidfds = ownership.ensure_pidfd_support()
        self.assertEqual("libc", self.pidfds.implementation)

    def test_signals_a_real_child(self):
        NativePidfdCompatibilityTests.terminate_through(self, self.pidfds)

    def test_exited_process_maps_to_process_lookup_error(self):
        child = spawn_sleeper(self)
        descriptor = self.pidfds.pidfd_open(child.pid, 0)
        try:
            child.kill()
            child.wait(10)
            with self.assertRaises(ProcessLookupError):
                self.pidfds.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
        finally:
            os.close(descriptor)
        with self.assertRaises(ProcessLookupError):
            self.pidfds.pidfd_open(child.pid, 0)

    def test_departed_descendant_is_skipped_not_raised(self):
        child = spawn_sleeper(self)
        child.kill()
        child.wait(10)
        delivered = set()
        with patch.object(ownership, "get_owned_descendants", return_value={child.pid: "birth"}), \
             patch.object(ownership, "get_process_identity", return_value="birth"):
            ownership.apply_descendant_signal(self.pidfds, signal.SIGKILL, delivered)
        self.assertEqual(set(), delivered)
