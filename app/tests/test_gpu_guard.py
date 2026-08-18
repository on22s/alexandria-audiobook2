"""Work must not step around the queue by accident.

An experiment's result looked wrong, so I ran LLM calls by hand to see what
the model was replying. A book generation held the GPU; the calls queued
against the same server, took attention from the job that had properly
acquired the lock, and timed out after two minutes having diagnosed nothing.

Nothing prevented that, and "remember not to" is not a mechanism.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from experiments.gpu_guard import gpu_is_busy, require_free_gpu


class GpuGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "gpu.lock")
        open(self.lock, "a").close()
        for name in ("ALEXANDRIA_GPU_LOCK_HELD", "ALEXANDRIA_ALLOW_CONTENTION"):
            os.environ.pop(name, None)

    def _hold(self):
        """A separate process holding the lock, as a real job does."""
        code = textwrap.dedent(f"""
            import fcntl, time
            h = open({self.lock!r}, "a")
            fcntl.flock(h, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(30)
        """)
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.stdout.readline().strip() == "held":
                return proc
            time.sleep(0.05)
        self.skipTest("could not acquire the test lock")

    def test_a_free_gpu_is_not_busy(self):
        self.assertFalse(gpu_is_busy(self.lock))

    def test_a_held_lock_reads_as_busy(self):
        self._hold()
        self.assertTrue(gpu_is_busy(self.lock))

    def test_a_hand_run_call_is_refused_while_a_job_runs(self):
        """THE DEFECT, in one line."""
        self._hold()
        with self.assertRaises(SystemExit) as caught:
            require_free_gpu("a probe", self.lock)
        message = str(caught.exception)
        self.assertIn("gpu_job.sh", message, "the refusal must say how to queue it")

    def test_the_queued_job_itself_is_allowed_through(self):
        # gpu_job.sh exports this into everything it starts; without the
        # exemption the guard would refuse the very jobs it protects.
        self._hold()
        os.environ["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
        self.addCleanup(os.environ.pop, "ALEXANDRIA_GPU_LOCK_HELD", None)
        require_free_gpu("a queued job", self.lock)

    def test_the_override_exists_for_a_deliberate_exception(self):
        self._hold()
        os.environ["ALEXANDRIA_ALLOW_CONTENTION"] = "1"
        self.addCleanup(os.environ.pop, "ALEXANDRIA_ALLOW_CONTENTION", None)
        require_free_gpu("a deliberate exception", self.lock)

    def test_a_missing_lock_file_is_not_a_reason_to_refuse(self):
        # No queue on this machine is not the same as a busy one; refusing
        # would break every clone that has never run a job.
        require_free_gpu("anything", os.path.join(self.tmp.name, "absent.lock"))

    def test_the_probe_does_not_hold_the_lock_it_tests(self):
        """Checking must not delay the job that owns the card."""
        self.assertFalse(gpu_is_busy(self.lock))
        self.assertFalse(gpu_is_busy(self.lock))
