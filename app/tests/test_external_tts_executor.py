"""Real blocked workers, queue cancellation and a native interpreter-exit proof."""
from concurrent.futures import TimeoutError
import builtins
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from external_tts_executor import ExternalTTSExecutor


class ExternalExecutorTests(unittest.TestCase):
    def test_repeated_timeouts_keep_one_pool_and_remove_cancelled_queue_entries(self):
        release = threading.Event(); arrived = threading.Event(); calls = []
        pool = ExternalTTSExecutor(1)
        def blocked():
            calls.append("active"); arrived.set(); release.wait(5)
            return "finished"
        running = pool.submit(blocked)
        try:
            self.assertTrue(arrived.wait(2))
            original = list(pool._threads)
            for _ in range(8):
                future = pool.submit(lambda: calls.append("queued"))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=.01)
                self.assertTrue(future.cancel())
                self.assertEqual(0, len(pool._jobs))
                self.assertEqual(original, pool._threads)
                self.assertEqual(1, sum(t.is_alive() for t in pool._threads))
            self.assertEqual(["active"], calls)
        finally:
            release.set(); pool.shutdown(wait=True, cancel_futures=True)
        self.assertEqual("finished", running.result())
        self.assertFalse(any(t.is_alive() for t in pool._threads))

    def test_success_exception_and_shutdown_preserve_real_future_semantics(self):
        pool = ExternalTTSExecutor(2)
        try:
            self.assertEqual(7, pool.submit(lambda a, b=0: a + b, 3, b=4).result(2))
            def fail():
                raise ValueError("provider failure")
            with self.assertRaisesRegex(ValueError, "provider failure"):
                pool.submit(fail).result(2)
            self.assertEqual(9, pool.submit(lambda: 9).result(2))
        finally:
            pool.shutdown()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            pool.submit(lambda: 0)

    def test_native_interpreter_can_exit_with_an_unreleased_application_worker(self):
        script = (
            "import threading\n"
            "from external_tts_executor import ExternalTTSExecutor\n"
            "pool=ExternalTTSExecutor(1)\n"
            "started=threading.Event()\n"
            "def block():\n"
            " started.set();threading.Event().wait()\n"
            "pool.submit(block)\n"
            "assert started.wait(2)\n"
            "print('native-exit-ready',flush=True)\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("native-exit-ready", result.stdout)

    def test_bounded_shutdown_reports_live_worker_and_can_be_retried(self):
        started, release = threading.Event(), threading.Event()
        pool = ExternalTTSExecutor(1)
        def blocked():
            started.set()
            release.wait(5)
        work = pool.submit(blocked)
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(builtins.TimeoutError, 'shutdown cleanup'):
                pool.shutdown(timeout=.05)
            self.assertTrue(pool._threads[0].is_alive())
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                pool.submit(lambda: None)
        finally:
            release.set()
            pool.shutdown(timeout=2)
        self.assertIsNone(work.result())
        self.assertFalse(pool._threads[0].is_alive())

    def test_worker_can_close_its_own_pool_without_self_join(self):
        pool = ExternalTTSExecutor(1)
        try:
            self.assertIsNone(pool.submit(lambda: pool.shutdown(timeout=1)).result(2))
        finally:
            pool.shutdown(timeout=2)
