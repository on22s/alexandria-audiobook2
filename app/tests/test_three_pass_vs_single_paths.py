"""Path handling in the three-pass-vs-single harness.

WHY THIS FILE EXISTS. Every arm of the PR #308 remeasurement failed on
2026-08-16 and again on 2026-08-17 with:

    FileNotFoundError: 'ab_test_runtime/results/.../inputs/index18.txt'

The arms are subprocesses launched with `cwd=APP`, so a relative `--inputs`
given at the repo root resolves against `app/` inside the child and disappears.
Both runs reported "FAILED rc=1" for both books, and the summary's own hint
said to check the LLM server - so two sessions chased a healthy engine for a
day over a path bug. The file existed the whole time; nothing asserted where
the harness looked for it.

THESE TESTS NEVER CALL main(). The first draft did, with the arms patched out,
and it still ran far enough to write the harness's DEFAULT --out - overwriting
`ab_test_runtime/experiments/three_pass_vs_single.json`, a committed four-book
result, with an empty failure. A test that destroys evidence while checking
path handling is a worse bug than the one it was written for. The resolution
logic is exercised directly instead.
"""
import os
import subprocess
import sys
import unittest

from experiments import three_pass_vs_single as harness


class PathResolutionTests(unittest.TestCase):
    """Relative paths must resolve against the INVOCATION directory."""

    def test_the_harness_resolves_its_three_path_arguments(self):
        source = open(harness.__file__, encoding="utf-8").read()
        for name in ("args.inputs = os.path.abspath",
                     "args.work = os.path.abspath",
                     "args.out = os.path.abspath"):
            self.assertIn(name, source, f"{name} missing; a relative path "
                                        "would vanish inside the arm")

    def test_resolution_happens_before_the_paths_are_used(self):
        # Absolutising after `makedirs(args.work)` or after the arm loop would
        # look correct and fix nothing.
        source = open(harness.__file__, encoding="utf-8").read()
        resolved_at = source.index("args.inputs = os.path.abspath")
        used_at = source.index("os.makedirs(args.work")
        self.assertLess(resolved_at, used_at,
                        "paths must be resolved before anything uses them")

    def test_abspath_uses_the_invocation_directory_not_the_repo(self):
        # The point is not merely "absolute" but "absolute against the CWD the
        # user typed it in". A REPO-relative join would be wrong when the
        # harness is run from anywhere else.
        self.assertEqual(os.path.join(os.getcwd(), "ab_test_runtime", "x"),
                         os.path.abspath(os.path.join("ab_test_runtime", "x")))


class ArmInvocationTests(unittest.TestCase):
    """The arms really do run from app/, which is why the above matters."""

    def test_the_harness_launches_arms_from_the_app_directory(self):
        source = open(harness.__file__, encoding="utf-8").read()
        self.assertIn("cwd=APP", source,
                      "if the arms stop running from app/, the absolute-path "
                      "resolution is no longer load-bearing and this test "
                      "should be revisited rather than deleted")

    def test_a_child_process_cannot_see_a_repo_relative_path_from_app(self):
        """The mechanism itself, demonstrated rather than asserted."""
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(harness.__file__))))
        app = os.path.join(repo, "app")
        relative = os.path.join("app", "experiments", "three_pass_vs_single.py")
        self.assertTrue(os.path.exists(os.path.join(repo, relative)),
                        "fixture premise: the path exists from the repo root")
        probe = subprocess.run(
            [sys.executable, "-c",
             f"import os,sys; sys.exit(0 if os.path.exists({relative!r}) else 3)"],
            cwd=app, capture_output=True, timeout=30)
        self.assertEqual(3, probe.returncode,
                         "a repo-relative path resolved from app/; the premise "
                         "of the absolute-path fix no longer holds")


class FailureAdviceTests(unittest.TestCase):
    """The old hint said "check the LLM server" for every failure shape."""

    def test_advice_points_at_the_log_before_the_server(self):
        source = open(harness.__file__, encoding="utf-8").read()
        advice_at = source.index("Read the failing arm's log")
        server_at = source.index("then check the server")
        self.assertLess(advice_at, server_at,
                        "the log must be offered before the server guess")

    def test_advice_survives_a_failure_that_has_no_arm(self):
        """A book skipped for a missing source is {book, error} with no arm.

        The first version of this advice indexed f['arm'] blindly and raised
        KeyError while explaining someone else's failure.
        """
        source = open(harness.__file__, encoding="utf-8").read()
        # The guard's existence is covered by indexing it below; a separate
        # assertIn was redundant.
        # The indexed form is fine INSIDE that guard - what raised KeyError was
        # reaching it unguarded - so assert the guard precedes the use rather
        # than banning the pattern outright.
        guard_at = source.index('if f.get("arm")')
        use_at = source.index("f['arm']}.log")
        self.assertLess(guard_at, use_at,
                        "f['arm'] is read before anything checks it exists")


if __name__ == "__main__":
    unittest.main()
