"""The untracked-harness gate must not count what runs WRITE.

The gate exists because a new experiment script is untracked for exactly as
long as it takes to write and run it, so an artifact could name a commit that
did not contain its code. #417 widened the scan from app/experiments and
run_chains to the whole tree, which was right for harnesses and wrong for
everything else: `ab_test_runtime/` holds an untracked virtualenv
(envs/ctc-align/.../site-packages, thousands of .py), three cloud_backup_*
trees carrying .py and .sh, and generated .html views.

On 2026-08-29 that made the dirty flag true on EVERY run and the queue refused
the night's first job in 0s. gpu_job.sh's own comment had already warned about
this shape - "Counting them made an earlier version of this flag true on every
run, which is the same as being false" - and the widening reintroduced it.

Both the shell gate and manifest.py must exclude ab_test_runtime/ and must
still see a harness anywhere in the code tree.
"""
import os
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def untracked_harness(extra_pathspec=True):
    """What the gate counts, run the way both implementations run it."""
    argv = ["git", "-C", REPO, "ls-files", "--others", "--exclude-standard"]
    if extra_pathspec:
        argv += ["--", ":(exclude)ab_test_runtime/*"]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
    return [n for n in out.splitlines()
            if n.endswith((".py", ".sh", ".js", ".html"))]


class DirtyGateIgnoresArtifactsTest(unittest.TestCase):

    def test_both_implementations_exclude_the_artifact_tree(self):
        """A pathspec in one and not the other is the drift Rule 15 forbids."""
        def read(*parts):
            with open(os.path.join(REPO, *parts), encoding="utf-8") as handle:
                return handle.read()
        shell = read("gpu_job.sh")
        python = read("app", "experiments", "manifest.py")
        for name, text in (("gpu_job.sh", shell), ("manifest.py", python)):
            with self.subTest(name):
                self.assertIn(":(exclude)ab_test_runtime/*", text,
                              f"{name} counts artifacts as harness dirt")

    def test_without_the_exclusion_an_artifact_makes_the_tree_dirty(self):
        """The two scans must DISAGREE about a file under ab_test_runtime/.

        Asserted on a file this test writes, not on whatever the working tree
        happens to contain: a fresh clone has no untracked artifacts, so a
        version of this that read ambient state skipped in CI - and
        verify_release counts one skipped test as a failure, so it took the
        whole run down (2026-08-30, this PR's first CI).
        """
        probe = os.path.join(REPO, "ab_test_runtime", "_dirty_gate_probe.py")
        self.assertFalse(os.path.exists(probe), "probe path is not free")
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("# written by a run, not by an author\n")
            relative = "ab_test_runtime/_dirty_gate_probe.py"
            self.assertIn(relative, untracked_harness(extra_pathspec=False),
                          "the unfiltered scan is what #417 shipped; it must "
                          "see this file, or this test proves nothing")
            self.assertNotIn(relative, untracked_harness(),
                             "the filtered scan must ignore it")
        finally:
            os.remove(probe)

    def test_a_harness_in_the_code_tree_is_still_caught(self):
        """417's real gain: reach beyond app/experiments and run_chains."""
        probe = os.path.join(REPO, "run_chains", "_dirty_gate_probe.sh")
        self.assertFalse(os.path.exists(probe), "probe path is not free")
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/bash\n")
            self.assertIn("run_chains/_dirty_gate_probe.sh", untracked_harness())
        finally:
            os.remove(probe)

if __name__ == "__main__":
    unittest.main()
