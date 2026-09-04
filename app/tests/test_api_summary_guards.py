"""The API-summary validators, which nothing had ever reached.

A trace over all 2,696 tests on 2026-09-04 found six branches of
verify_release.validate_api_summary never executed. They are the gate between
"the API suite ran" and "the API suite passed", and an unexercised gate is
indistinguishable from an absent one - a summary claiming success with a
failed test in it would have been believed.

Each test below drives ONE branch and asserts its specific message, so a
future refactor that silently drops a branch fails here rather than going
quietly green.
"""
import unittest

import verify_release


def _summary(**over):
    base = {
        "schema_version": 1,
        "mode": "quick",
        "tests": [{"name": "t1", "status": "passed", "requires_full": False}],
        "counts": {"passed": 1, "failed": 0, "skipped": 0, "total": 1},
    }
    base.update(over)
    return base


class ApiSummaryGuards(unittest.TestCase):
    def test_a_faithful_summary_is_accepted(self):
        """The check must also pass something valid, or it proves nothing."""
        verify_release.validate_api_summary(_summary(), False)

    def test_wrong_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid API summary schema or mode"):
            verify_release.validate_api_summary(_summary(mode="full"), False)

    def test_wrong_schema_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid API summary schema or mode"):
            verify_release.validate_api_summary(_summary(schema_version=2), False)

    def test_missing_tests_or_counts_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing tests or counts"):
            verify_release.validate_api_summary(_summary(tests=None), False)
        with self.assertRaisesRegex(ValueError, "missing tests or counts"):
            verify_release.validate_api_summary(_summary(counts=None), False)

    def test_duplicate_or_empty_names_are_rejected(self):
        dup = [{"name": "t", "status": "passed", "requires_full": False},
               {"name": "t", "status": "passed", "requires_full": False}]
        with self.assertRaisesRegex(ValueError, "non-empty and unique"):
            verify_release.validate_api_summary(
                _summary(tests=dup, counts={"passed": 2, "failed": 0,
                                            "skipped": 0, "total": 2}), False)

    def test_requires_full_must_be_a_bool(self):
        t = [{"name": "t1", "status": "passed", "requires_full": "no"}]
        with self.assertRaisesRegex(ValueError, "must declare requires_full"):
            verify_release.validate_api_summary(_summary(tests=t), False)

    def test_an_invalid_status_is_rejected(self):
        t = [{"name": "t1", "status": "errored", "requires_full": False}]
        with self.assertRaisesRegex(ValueError, "invalid test status"):
            verify_release.validate_api_summary(
                _summary(tests=t, counts={"passed": 0, "failed": 0,
                                          "skipped": 0, "total": 1}), False)

    def test_a_failed_test_is_reported_not_swallowed(self):
        """The branch that matters most: a summary that ran and FAILED."""
        t = [{"name": "t1", "status": "failed", "requires_full": False}]
        with self.assertRaisesRegex(ValueError, "reported failed tests: t1"):
            verify_release.validate_api_summary(
                _summary(tests=t, counts={"passed": 0, "failed": 1,
                                          "skipped": 0, "total": 1}), False)


if __name__ == "__main__":
    unittest.main()
