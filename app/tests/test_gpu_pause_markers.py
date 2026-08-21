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
            "LOCK_FAILED", "INTERRUPTED ", "STOPPED  "]
# These are written and the job PROCEEDS, so they must not clear it.
NON_TERMINAL = ["DIRTY_RUN", "LLM_UNCHECKED", "VRAM_UNKNOWN", "HELD     "]


@unittest.skipUnless(os.path.exists(SCRIPT) and shutil.which("pgrep"),
                     "needs gpu_pause.sh and pgrep")
class TerminalMarkerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # A process that `job_is_live` will match, so the marker set is what
        # decides the answer rather than the liveness gate.
        self.fake = subprocess.Popen(
            ["bash", "-c", 'exec -a "/x/gpu_job.sh jobA --fake" sleep 30'])
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
