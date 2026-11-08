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
import json
import unittest
from pathlib import Path
from unittest.mock import patch

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
        for fields in ({"name": ""}, {}, {"name": None}):
            with self.subTest(fields=fields):
                tests = [{"name": "valid", "status": "passed", "requires_full": False},
                         {"status": "passed", "requires_full": False, **fields}]
                with self.assertRaisesRegex(ValueError, "non-empty and unique"):
                    verify_release.validate_api_summary(
                        _summary(tests=tests, counts={"passed": 2, "failed": 0,
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




class EmptyAndMalformedApiSummaryTests(unittest.TestCase):
    def test_empty_suite_is_rejected_in_quick_and_full_modes(self):
        for full in (False, True):
            summary = {'schema_version':1, 'mode':'full' if full else 'quick',
                       'tests':[], 'counts':{'passed':0, 'failed':0, 'skipped':0, 'total':0}}
            with self.subTest(full=full), self.assertRaisesRegex(ValueError, 'no tests'):
                verify_release.validate_api_summary(summary, full)

    def test_non_object_summary_is_a_controlled_validation_error(self):
        for summary in ([], None, 'summary', True, 7):
            with self.subTest(summary=summary), self.assertRaisesRegex(ValueError, 'object'):
                verify_release.validate_api_summary(summary, False)


class StrictApiSummaryTypeTests(unittest.TestCase):
    def test_valid_quick_and_full_integer_summaries_are_accepted(self):
        quick = _summary()
        self.assertEqual(quick["counts"],
                         verify_release.validate_api_summary(quick, False))
        full = _summary(mode="full", tests=[{"name": "t1", "status": "passed",
                                             "requires_full": True}])
        self.assertEqual(full["counts"],
                         verify_release.validate_api_summary(full, True))

    def test_schema_version_must_be_actual_int_not_bool_or_float(self):
        for value in (True, 1.0):
            with self.subTest(schema_version=value):
                with self.assertRaisesRegex(ValueError, "Invalid API summary schema or mode"):
                    verify_release.validate_api_summary(
                        _summary(schema_version=value), False)

    def test_every_count_must_be_actual_int_not_bool_or_float(self):
        base_counts = dict(_summary()["counts"])
        for key, original in base_counts.items():
            for value in (bool(original), float(original)):
                with self.subTest(count=key, value=value):
                    counts = dict(base_counts)
                    counts[key] = value
                    with self.assertRaisesRegex(ValueError, "nonnegative integers"):
                        verify_release.validate_api_summary(
                            _summary(counts=counts), False)

    def test_malformed_counts_are_rejected(self):
        base_counts = dict(_summary()["counts"])
        cases = []
        for key in base_counts:
            negative = dict(base_counts)
            negative[key] = -1
            cases.append((f"negative {key}", negative))
            missing = dict(base_counts)
            del missing[key]
            cases.append((f"missing {key}", missing))
            non_numeric = dict(base_counts)
            non_numeric[key] = "1"
            cases.append((f"non-numeric {key}", non_numeric))
        extra = dict(base_counts)
        extra["extra"] = 0
        cases.append(("extra key", extra))
        for label, counts in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    verify_release.validate_api_summary(
                        _summary(counts=counts), False)

    def test_run_api_suite_loads_valid_json_summary_for_quick_and_full_modes(self):
        for full in (False, True):
            with self.subTest(full=full):
                tests = [
                    {"name": "always", "status": "passed", "requires_full": False},
                    {"name": "full_only",
                     "status": "passed" if full else "skipped",
                     "requires_full": True},
                ]
                if not full:
                    tests[1]["reason"] = "requires --full"
                counts = {"passed": 2 if full else 1, "failed": 0,
                          "skipped": 0 if full else 1, "total": 2}
                summary = _summary(mode="full" if full else "quick",
                                   tests=tests, counts=counts)
                calls = []

                def write_summary(label, command, _cwd):
                    calls.append((label, list(command)))
                    Path(command[command.index("--json-summary") + 1]).write_text(
                        json.dumps(summary), encoding="utf-8")

                with patch.object(verify_release, "run_report_command",
                                  side_effect=write_summary):
                    result = verify_release.run_api_suite(Path("."), full)

                expected_skips = ([] if full else
                                  [{"name": "full_only", "reason": "requires --full"}])
                self.assertEqual({"counts": counts, "skips": expected_skips}, result)
                self.assertEqual(1, len(calls))
                label, command = calls[0]
                self.assertEqual("Full isolated API suite" if full else
                                 "Quick isolated API suite", label)
                self.assertEqual(full, "--full" in command)

    def test_run_api_suite_loads_json_and_rejects_malformed_types(self):
        malformed = _summary(schema_version=True)
        def write_summary(_label, command, _cwd):
            Path(command[command.index("--json-summary") + 1]).write_text(
                json.dumps(malformed), encoding="utf-8")
        with patch.object(verify_release, "run_report_command",
                          side_effect=write_summary):
            with self.assertRaisesRegex(ValueError, "Invalid API summary schema or mode"):
                verify_release.run_api_suite(Path("."), False)


if __name__ == "__main__":
    unittest.main()
