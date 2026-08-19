"""Work must not step around the queue by accident.

An experiment's result looked wrong, so I ran LLM calls by hand to see what
the model was replying. A book generation held the GPU; the calls queued
against the same server, took attention from the job that had properly
acquired the lock, and timed out after two minutes having diagnosed nothing.

Nothing prevented that, and "remember not to" is not a mechanism.
"""
import glob
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


class LockPathAgreementTest(unittest.TestCase):
    """One lock, or the queue does not serialise at all.

    Measured 2026-08-19 with a chain job running: the repo lock was HELD while
    both $HOME/.gpu.lock and $HOME/.alexandria_gpu.lock were FREE. gpu_job.sh
    defaulted to the first of those, so a hand-run job would have taken a
    different lock, found it free, and run a second job on the card.
    """

    def test_the_guard_and_gpu_job_agree_on_the_default_lock(self):
        """Not by parsing - by running the shell and taking its answer."""
        from experiments.gpu_guard import default_lock
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        spoken = subprocess.run(
            ["bash", os.path.join(repo, "gpu_job.sh"), "--print-lock"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(0, spoken.returncode, spoken.stderr)
        self.assertEqual(spoken.stdout.strip(), default_lock())
        self.assertTrue(default_lock().startswith("/"), default_lock())
        self.assertTrue(default_lock().endswith(
            "ab_test_runtime/logs/alexandria_gpu.lock"), default_lock())

    def test_the_default_is_the_same_from_any_working_directory(self):
        """`dirname "$0"` is relative when invoked as ./gpu_job.sh, and a
        relative lock is a different FILE for a caller elsewhere."""
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        answers = set()
        for cwd in (repo, tempfile.gettempdir()):
            answers.add(subprocess.run(
                ["bash", os.path.join(repo, "gpu_job.sh"), "--print-lock"],
                capture_output=True, text=True, cwd=cwd,
                timeout=60).stdout.strip())
        self.assertEqual(1, len(answers), answers)

    def test_an_unreadable_gpu_job_raises_instead_of_guessing(self):
        """The old parser returned a plausible path when it could not read the
        script. A guessed lock is the exact failure this module exists to
        prevent, so not knowing must be an error."""
        import experiments.gpu_guard as guard
        default_lock_fn = guard.default_lock
        default_lock_fn.cache_clear()
        original = guard.GPU_JOB
        guard.GPU_JOB = os.path.join(self.tmp.name if hasattr(self, "tmp")
                                     else tempfile.gettempdir(), "absent.sh")
        try:
            with self.assertRaises(Exception):
                default_lock_fn()
        finally:
            guard.GPU_JOB = original
            default_lock_fn.cache_clear()

    def test_no_chain_names_a_lock_other_than_the_shared_one(self):
        """The first version of this test asked only that the FILENAME was not
        `gpu.lock`, which let $HOME/.alexandria_gpu.lock through - and 15
        chains were exporting exactly that. Compare the resolved path.
        """
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        shared = subprocess.run(
            ["bash", os.path.join(repo, "gpu_job.sh"), "--print-lock"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        runtime = os.path.join(repo, "ab_test_runtime")
        wrong = []
        for path in sorted(glob.glob(os.path.join(repo, "run_chains", "*.sh"))
                           + glob.glob(os.path.join(repo, "*.sh"))):
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    if "GPU_LOCK=" not in line or "ALEXANDRIA_GPU_LOCK" in line:
                        continue
                    if line.lstrip().startswith("#"):
                        continue
                    # $runtime is the only variable these lines use.
                    resolved = (line.split("GPU_LOCK=", 1)[1].split()[0]
                                .strip('"').replace("$runtime", runtime))
                    if os.path.realpath(resolved) != os.path.realpath(shared):
                        wrong.append("%s:%d %s" % (os.path.basename(path),
                                                   number, resolved))
        self.assertEqual([], wrong,
                         "these name a lock that does not serialise against "
                         "the one the queue holds:\n  " + "\n  ".join(wrong))

class FailClosedTest(unittest.TestCase):
    """An unopenable lock is a configuration error, not an idle GPU."""

    def test_an_unreadable_lock_reads_as_busy(self):
        from experiments.gpu_guard import gpu_is_busy
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "locked", "gpu.lock")
            os.makedirs(os.path.dirname(path))
            open(path, "a").close()
            os.chmod(os.path.dirname(path), 0o500)
            os.chmod(path, 0o000)
            try:
                if os.access(path, os.W_OK):
                    self.skipTest("running as a user that ignores file modes")
                self.assertTrue(gpu_is_busy(path))
            finally:
                os.chmod(os.path.dirname(path), 0o700)
                os.chmod(path, 0o600)

    def test_a_lock_that_does_not_exist_is_not_busy(self):
        from experiments.gpu_guard import gpu_is_busy
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(gpu_is_busy(os.path.join(tmp, "absent.lock")))


class StaleSentinelTest(unittest.TestCase):
    """The exemption belongs to a job, not to a terminal.

    Chains re-exec under ALEXANDRIA_GPU_LOCK_HELD=1, so a shell opened from
    inside one inherits it for the rest of its life - including after that job
    ends and another takes the card.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "gpu.lock")
        open(self.lock, "a").close()
        for name in ("ALEXANDRIA_GPU_LOCK_HELD", "ALEXANDRIA_GPU_LOCK_PID",
                     "ALEXANDRIA_ALLOW_CONTENTION"):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)

    def _hold(self):
        code = ("import fcntl,time;h=open(%r,'a');fcntl.flock(h,fcntl.LOCK_EX);"
                "print('held',flush=True);time.sleep(30)" % self.lock)
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.stdout.readline().strip() == "held":
                return proc
            time.sleep(0.05)
        self.skipTest("could not acquire the test lock")

    def test_a_sentinel_from_a_dead_job_no_longer_exempts(self):
        from experiments.gpu_guard import require_free_gpu
        self._hold()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        os.environ["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
        os.environ["ALEXANDRIA_GPU_LOCK_PID"] = str(dead.pid)
        with self.assertRaises(SystemExit):
            require_free_gpu("a shell that outlived its chain", self.lock)

    def test_a_live_job_is_still_exempt(self):
        from experiments.gpu_guard import require_free_gpu
        self._hold()
        os.environ["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
        os.environ["ALEXANDRIA_GPU_LOCK_PID"] = str(os.getpid())
        require_free_gpu("the queued job itself", self.lock)
