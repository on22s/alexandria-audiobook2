"""Every terminal outcome gpu_job.sh writes must be one gpu_pause.sh clears.

#401 fixed one instance: NO_LLM was added to the writer and not the reader, so
a preflight-refused job was reported as running forever and anything waiting
for a free card waited indefinitely. That was a DRIFT between two files, and
fixing the instance does not stop the next marker drifting the same way.

This compares the two directly. It reads both scripts as text rather than
running them, so it costs nothing and cannot be skipped for want of a GPU.

TERMINAL means the job will not run. gpu_job.sh also writes progress markers -
DIRTY_RUN, LLM_UNCHECKED, VRAM_UNKNOWN, HELD - and every one of those PROCEEDS
to run the job, so they must NOT clear it. Both directions are asserted:
a terminal marker missing from the reader is the #401 bug; a progress marker
present in it would report a running job as finished, which is worse.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOB = os.path.join(REPO, "gpu_job.sh")
PAUSE = os.path.join(REPO, "gpu_pause.sh")

# Classified by reading gpu_job.sh: does the job still run after this line?
TERMINAL = {"OK", "FAILED", "REFUSED", "NO_VRAM", "NO_LLM", "KILLED",
            "LOCK_FAILED", "INTERRUPTED", "STOPPED"}
PROCEEDS = {"DIRTY_RUN", "LLM_UNCHECKED", "VRAM_UNKNOWN", "HELD", "START",
            "QUEUED", "IDENT", "RELEASED"}


def markers_written():
    """-> every marker gpu_job.sh writes to the queue log."""
    with open(JOB, encoding="utf-8") as fh:
        text = fh.read()
    return set(re.findall(r'\$\(stamp\)\s+([A-Z_]+)', text))


def markers_cleared():
    """-> every marker gpu_pause.sh treats as terminal."""
    with open(PAUSE, encoding="utf-8") as fh:
        for line in fh:
            if '{name=""}' in line and "/" in line:
                body = line[line.index("/") + 1:line.rindex("/")]
                return {p.strip() for p in body.split("|") if p.strip()}
    raise AssertionError("no terminal-marker pattern found in gpu_pause.sh")


@unittest.skipUnless(os.path.exists(JOB) and os.path.exists(PAUSE),
                     "needs gpu_job.sh and gpu_pause.sh")
class MarkerDriftTests(unittest.TestCase):
    def test_every_marker_is_classified(self):
        """A new marker must be deliberately sorted, not silently ignored."""
        unknown = markers_written() - TERMINAL - PROCEEDS
        self.assertEqual(unknown, set(),
                         "gpu_job.sh writes markers this test has never been "
                         "told about: %s. Decide whether each is terminal "
                         "(the job will not run) and add it above." % sorted(unknown))

    def test_every_terminal_marker_is_cleared_by_gpu_pause(self):
        """The #401 bug: a refused job that goes on looking busy forever."""
        written = markers_written()
        cleared = markers_cleared()
        missing = sorted(m for m in TERMINAL & written
                         if not any(c.startswith(m) for c in cleared))
        self.assertEqual(missing, [],
                         "gpu_job.sh writes these terminal markers but "
                         "gpu_pause.sh never clears them, so the job is "
                         "reported as running forever: %s" % missing)

    def test_no_marker_that_still_runs_the_job_is_cleared(self):
        """The inverse, which would be worse: a live job reported finished."""
        cleared = markers_cleared()
        wrong = sorted(m for m in PROCEEDS
                       if any(c.startswith(m) for c in cleared))
        self.assertEqual(wrong, [],
                         "gpu_pause.sh clears markers after which the job "
                         "STILL RUNS, so a live job reads as finished: %s" % wrong)

    def test_the_comparison_is_not_vacuous(self):
        # If either parser silently returned nothing, every assertion above
        # would pass. Both must find real content.
        self.assertGreaterEqual(len(markers_written()), 10)
        self.assertGreaterEqual(len(markers_cleared()), 5)
        self.assertIn("NO_LLM", markers_written())
