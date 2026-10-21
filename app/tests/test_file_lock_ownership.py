"""Exercise lock ownership using real child processes and filesystem artifacts."""
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from utils import file_lock


class FileLockOwnershipTests(unittest.TestCase):
    def test_preopened_lock_preserves_caller_handle_and_never_reopens_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state"))
            with open(target + ".lock", "a+b") as handle:
                with patch("builtins.open", side_effect=AssertionError("must use captured handle")):
                    with file_lock(target, timeout=0, lock_handle=handle):
                        self.assertFalse(handle.closed)
                self.assertFalse(handle.closed)
                with file_lock(target, timeout=0):
                    pass

    def test_independent_preopened_handle_contends_with_ordinary_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state"))
            with open(target + ".lock", "a+b") as handle:
                with file_lock(target, timeout=0):
                    with self.assertRaises(TimeoutError):
                        with file_lock(target, timeout=0, lock_handle=handle):
                            self.fail("another descriptor owns the lock")
                with self.assertRaisesRegex(ValueError, "caller failed"):
                    with file_lock(target, timeout=0, lock_handle=handle):
                        with self.assertRaises(TimeoutError):
                            with file_lock(target, timeout=0):
                                self.fail("captured descriptor owns the lock")
                        raise ValueError("caller failed")
                self.assertFalse(handle.closed)
                with file_lock(target, timeout=0):
                    pass

    def test_preopened_descriptor_excludes_a_real_child_process(self):
        code = """import sys
from utils import file_lock
try:
 with file_lock(sys.argv[1], timeout=0):
  print('acquired')
except TimeoutError:
 print('blocked')
"""
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state"))
            def child_result():
                return subprocess.run([sys.executable, "-c", code, target],
                                      capture_output=True, text=True, check=True,
                                      timeout=5).stdout.strip()
            with open(target + ".lock", "a+b") as handle:
                inode = Path(target + ".lock").stat().st_ino
                with file_lock(target, timeout=0, lock_handle=handle):
                    self.assertEqual("blocked", child_result())
                self.assertEqual("acquired", child_result())
                self.assertEqual(inode, Path(target + ".lock").stat().st_ino)

    def test_aged_live_holder_cannot_be_reaped_and_timeout_does_not_remove_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state.json"))
            with file_lock(target):
                lock = Path(target + ".lock")
                os.utime(lock, (1, 1))
                inode = lock.stat().st_ino
                with self.assertRaises(TimeoutError):
                    with file_lock(target, timeout=0.08, stale_after=0):
                        self.fail("Entered while another live holder owns the lock")
                self.assertEqual(inode, lock.stat().st_ino)
            # Stable lock inode is essential: unlink/recreate can split waiters.
            self.assertTrue(lock.exists())
            with file_lock(target, timeout=0, stale_after=0):
                self.assertEqual(inode, lock.stat().st_ino)

    @unittest.skipUnless(hasattr(os, "waitid") and hasattr(os, "WNOWAIT"), "requires POSIX waitid without reaping")
    def test_process_death_releases_kernel_ownership_without_reaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state.json"))
            code = """import sys,time
from utils import file_lock
with file_lock(sys.argv[1]):
 print('held',flush=True)
 time.sleep(60)
"""
            child = subprocess.Popen([sys.executable, "-c", code, target],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
            try:
                self.assertEqual("held", child.stdout.readline().strip())
                inode = Path(target + ".lock").stat().st_ino
                with self.assertRaises(TimeoutError):
                    with file_lock(target, timeout=0.08, stale_after=0):
                        self.fail("Child owns this lock")
                os.kill(child.pid, signal.SIGKILL)
                observed = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                self.assertEqual(child.pid, observed.si_pid)
                self.assertEqual(signal.SIGKILL, observed.si_status)
                self.assertIsNone(child.returncode)
                os.kill(child.pid, 0)  # Still an unreaped kernel process.
                with file_lock(target, timeout=1, stale_after=10**9):
                    self.assertEqual(inode, Path(target + ".lock").stat().st_ino)
            finally:
                try:
                    os.kill(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

    def test_waiting_threads_preserve_all_read_modify_write_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "counter")
            target.write_text("0")
            def increment(_):
                for _ in range(12):
                    with file_lock(str(target), timeout=5, stale_after=0):
                        value = int(target.read_text())
                        time.sleep(0.001)
                        target.write_text(str(value + 1))
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(increment, range(5)))
            self.assertEqual("60", target.read_text())

    def test_exceptions_release_and_old_empty_markers_need_no_age_based_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp, "state"))
            Path(target + ".lock").touch()
            with patch("utils.os.remove", side_effect=AssertionError("must not unlink lock")):
                with self.assertRaisesRegex(ValueError, "caller failed"):
                    with file_lock(target, timeout=0):
                        raise ValueError("caller failed")
                with file_lock(target, timeout=0):
                    pass
