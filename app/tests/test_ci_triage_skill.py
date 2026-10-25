"""The aa2-ci-triage skill's log parser finds the failing tests and nothing else.

Fixture lines copy the shape of real `gh run view --job <id> --log-failed`
output from this repo's CI, including the two log lines that look like test
failures and are not (an application ERROR log, and a refusal message).
"""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).parent.parent.parent / ".claude" / "skills" / "aa2-ci-triage" / "ci_failures.py"
spec = importlib.util.spec_from_file_location("ci_failures", SCRIPT)
ci_failures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_failures)


def as_gh_log(*lines, job="unit (2/3)", step="Shared release verifier"):
    return [f"{job}\t{step}\t2026-10-24T22:06:40.1234567Z {line}" for line in lines]


def parse(*lines):
    return [ci_failures.get_log_text(raw) for raw in as_gh_log(*lines)]


SEP = "=" * 70
DASH = "-" * 70


class JobFailureParsingTests(unittest.TestCase):
    def test_failing_tests_subtests_and_first_error_line_are_found(self):
        lines = parse(
            "test_ok (tests.test_a.A) ... ok",
            "ERROR:project:chunks.json is corrupted; backing it up and regenerating.",
            "ERROR: naming refused: publication recovery is required: run app/x.py",
            "ValueError: Adapter metadata does not match its weights",
            SEP,
            "FAIL: test_seed (tests.test_b.B) (seed=0, speaker='DESIGN')",
            DASH, "Traceback (most recent call last):",
            "AssertionError: {'completed': [5]} != {'completed': []}",
            SEP,
            "FAIL: test_seed (tests.test_b.B) (seed=-1, speaker='GROUP')",
            DASH, "AssertionError: second subtest detail is not used",
            SEP,
            "ERROR: test_preview (tests.test_c.C)",
            DASH, "TypeError: preview() got an unexpected keyword argument 'preview_path'",
            DASH, "Ran 3173 tests in 666.563s", "", "FAILED (failures=6, errors=5)",
            "ValueError: a later stray line outside any block")
        failures, summary = ci_failures.get_job_failures(lines)
        self.assertEqual({("test_seed", "tests.test_b.B"), ("test_preview", "tests.test_c.C")}, set(failures))
        seed = failures[("test_seed", "tests.test_b.B")]
        self.assertEqual(("FAIL", ["seed=0, speaker='DESIGN'", "seed=-1, speaker='GROUP'"]),
                         (seed["kind"], seed["subtests"]))
        self.assertEqual("AssertionError: {'completed': [5]} != {'completed': []}", seed["detail"])
        preview = failures[("test_preview", "tests.test_c.C")]
        self.assertEqual(("ERROR", []), (preview["kind"], preview["subtests"]))
        self.assertIn("unexpected keyword argument 'preview_path'", preview["detail"])
        self.assertEqual(["Ran 3173 tests in 666.563s", "FAILED (failures=6, errors=5)"], summary)

    def test_text_after_a_block_never_becomes_a_failure_s_cause(self):
        # The last failure printed no error line; noise after the summary, or
        # between blocks, must not be reported as why it failed.
        lines = parse(SEP, "FAIL: test_quiet (tests.test_e.E)", DASH, "Traceback (most recent call last):",
                      DASH, "Ran 12 tests in 1.0s", "FAILED (failures=1)",
                      "ValueError: printed by a later CI step")
        failures, _summary = ci_failures.get_job_failures(lines)
        self.assertEqual("", failures[("test_quiet", "tests.test_e.E")]["detail"])
        lines = parse(SEP, "FAIL: test_quiet (tests.test_e.E)", DASH, "Traceback (most recent call last):",
                      SEP, "ValueError: between blocks", "FAIL: test_next (tests.test_e.E)", DASH,
                      "AssertionError: next")
        failures, _summary = ci_failures.get_job_failures(lines)
        self.assertEqual(("", "AssertionError: next"), (failures[("test_quiet", "tests.test_e.E")]["detail"],
                                                       failures[("test_next", "tests.test_e.E")]["detail"]))

    def test_a_job_with_no_test_failure_reports_its_error_annotations(self):
        lines = parse("Ran 4545 tests in 361.369s", "OK",
                      "##[error]    raise ValueError('Adapter metadata does not match its weights')",
                      "##[error]Process completed with exit code 1.",
                      "##[error]The base commit passes; this pull request introduced the verifier failure.")
        self.assertEqual(({}, ["Ran 4545 tests in 361.369s", "OK"]), ci_failures.get_job_failures(lines))
        self.assertEqual(["Process completed with exit code 1.",
                          "The base commit passes; this pull request introduced the verifier failure."],
                         ci_failures.get_error_lines(lines))

    def test_timestamp_and_job_prefix_are_removed_before_matching(self):
        raw = as_gh_log("FAIL: test_x (tests.test_d.D)")[0]
        self.assertTrue(raw.startswith("unit (2/3)\tShared release verifier\t2026-"))
        self.assertIsNone(ci_failures.TEST_HEADER.match(raw))
        self.assertEqual(("test_x", "tests.test_d.D"),
                         ci_failures.TEST_HEADER.match(parse("FAIL: test_x (tests.test_d.D)")[0]).group(2, 3))


if __name__ == "__main__":
    unittest.main()
