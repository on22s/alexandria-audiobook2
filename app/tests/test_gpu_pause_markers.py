"""gpu_pause.sh must stop reporting a job that will never run.

The terminal-marker set in `logged_job` has to track every outcome gpu_job.sh
can write. NO_LLM was added to the writer and not to the reader, so a
preflight refusal (exit 6) left the job looking busy - on 2026-08-21
allrows_dot_tail was refused for a missing llama-server and `status` went on
naming it as the running job, which is precisely the "confident wrong answer"
the function's own comment forbids.

These tests need a LIVE process whose command line matches the wrapper.
Without one, `job_is_live` returns nothing and every case reads `none`,
which is how a first version of this test passed against the unfixed script.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "gpu_pause.sh")

# Written by gpu_job.sh. Terminal means THE JOB WILL NOT RUN.
TERMINAL = ["OK       ", "FAILED   ", "NO_VRAM  ", "NO_LLM   ", "KILLED   ",
            "LOCK_FAILED", "PENDING_FAILED", "INTERRUPTED ", "STOPPED  "]
# These are written and the job PROCEEDS, so they must not clear it.
NON_TERMINAL = ["DIRTY_RUN", "LLM_UNCHECKED", "VRAM_UNKNOWN", "HELD     "]


@unittest.skipUnless(os.path.exists(SCRIPT) and shutil.which("pgrep"),
                     "needs gpu_pause.sh and pgrep")
class TerminalMarkerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # A process that `job_is_live` will match, so the marker set is what
        # decides the answer rather than the liveness gate.
        wrapper = os.path.join(self.dir, "gpu_job.sh")
        with open(wrapper, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\nsleep 30\n")
        os.chmod(wrapper, 0o755)
        self.fake = subprocess.Popen([wrapper, "jobA", "--fake"])
        time.sleep(1.0)

    def tearDown(self):
        self.fake.kill()
        self.fake.wait(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _status(self, marker):
        log = os.path.join(self.dir, "q.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("2026-08-21T20:00:01Z START    jobA\n")
            if marker:
                fh.write("2026-08-21T20:00:02Z %s jobA (x)\n" % marker)
        out = subprocess.run(
            [SCRIPT, "status"], capture_output=True, text=True,
            env={**os.environ, "GPU_QLOG": log,
                 "GPU_PAUSE_FLAG": os.path.join(self.dir, "flag")}).stdout
        for line in out.splitlines():
            if line.startswith("running job:"):
                return line.split(":", 1)[1].strip()
        self.fail("no `running job:` line in:\n%s" % out)

    def test_a_bare_start_reads_as_running(self):
        # Guards the fixture itself: if this said `none`, every other
        # assertion below would pass for the wrong reason.
        self.assertEqual(self._status(None), "jobA")

    def test_every_terminal_marker_clears_the_job(self):
        for marker in TERMINAL:
            self.assertEqual(self._status(marker), "none", marker)

    def test_no_llm_clears_the_job(self):
        # The specific regression: gpu_job.sh exits 6 on a preflight failure,
        # so the job never runs and must not be reported as running.
        self.assertEqual(self._status("NO_LLM   "), "none")

    def test_markers_that_still_run_the_job_do_not_clear_it(self):
        for marker in NON_TERMINAL:
            self.assertEqual(self._status(marker), "jobA", marker)


class SpacedJobNameTests(unittest.TestCase):
    def test_actual_status_retains_full_live_name_and_terminal_markers(self):
        import signal
        from pathlib import Path
        for name in ("goal 13", "goal  13", "goal_13"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wrapper = root / "gpu_job.sh"
                wrapper.write_text('#!/bin/bash\nprintf ready > "$READY_PATH"\nsleep 30\n')
                wrapper.chmod(0o755)
                rocm = root / "rocm-smi"
                rocm.write_text('#!/bin/sh\nprintf "total used memory: 0\\n"\n')
                rocm.chmod(0o755)
                ready = root / "ready"
                log = root / "q.log"
                env = {**os.environ, "READY_PATH": str(ready), "GPU_QLOG": str(log),
                       "GPU_PAUSE_FLAG": str(root / "flag"),
                       "GPU_PENDING_DIR": str(root / "pending"),
                       "PATH": tmp + os.pathsep + os.environ["PATH"]}
                child = subprocess.Popen([str(wrapper), name], env=env, start_new_session=True)
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "fixture process must be live before status")
                    self.assertIsNone(child.poll())
                    for marker in (None, "HELD     ", "OK       ", "NO_LLM   "):
                        text = f"2026-09-30T14:00:00Z START    {name}\n"
                        if marker:
                            text += f"2026-09-30T14:00:01Z {marker} {name}\n"
                        log.write_text(text)
                        before = log.read_bytes()
                        result = subprocess.run([SCRIPT, "status"], env=env,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(0, result.returncode, result.stderr)
                        expected = name if marker in (None, "HELD     ") else "none"
                        self.assertIn("running job: " + expected + "\n", result.stdout)
                        self.assertEqual(before, log.read_bytes())
                        self.assertFalse((root / "flag").exists())
                finally:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=5)
