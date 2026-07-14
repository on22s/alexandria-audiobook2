import unittest
from unittest.mock import patch

import test_api


class ApiSummaryTests(unittest.TestCase):
    def setUp(self):
        self.state = (
            test_api.FULL_MODE,
            dict(test_api.results),
            list(test_api.failures),
            list(test_api.test_results),
        )
        test_api.FULL_MODE = False
        test_api.results.update(passed=0, failed=0, skipped=0)
        test_api.failures.clear()
        test_api.test_results.clear()

    def tearDown(self):
        full_mode, results, failures, test_results = self.state
        test_api.FULL_MODE = full_mode
        test_api.results.clear()
        test_api.results.update(results)
        test_api.failures[:] = failures
        test_api.test_results[:] = test_results

    def test_json_summary_records_ordered_test_identities_statuses_and_reasons(self):
        test_api.run_test("passes", lambda: None)
        test_api.run_test("full_only", lambda: None, requires_full=True)
        test_api.run_test("runtime_skip", self._skip)
        test_api.run_test("fails", self._fail)
        test_api.run_test("errors", self._error)

        self.assertEqual(
            {
                "schema_version": 1,
                "mode": "quick",
                "counts": {"passed": 1, "failed": 2, "skipped": 2, "total": 5},
                "tests": [
                    {"name": "passes", "status": "passed"},
                    {"name": "full_only", "status": "skipped", "reason": "requires --full"},
                    {"name": "runtime_skip", "status": "skipped", "reason": "not available"},
                    {"name": "fails", "status": "failed", "reason": "bad result"},
                    {"name": "errors", "status": "failed", "reason": "ValueError: broken"},
                ],
            },
            test_api.get_json_summary(),
        )

    @staticmethod
    def _skip():
        raise test_api.TestFailure("SKIP: not available")

    @staticmethod
    def _fail():
        raise test_api.TestFailure("bad result")

    @staticmethod
    def _error():
        raise ValueError("broken")

    def test_main_writes_summary_before_exiting_after_a_failure(self):
        def run_failed_suite():
            test_api.run_test("failure", self._fail)

        with patch.object(test_api, "run_all_tests", side_effect=run_failed_suite), \
             patch.object(test_api, "cleanup"), \
             patch.object(test_api, "atomic_json_write") as write_json, \
             patch("sys.argv", ["test_api.py", "--json-summary", "summary.json"]):
            with self.assertRaisesRegex(SystemExit, "1"):
                test_api.main()

        write_json.assert_called_once_with(test_api.get_json_summary(), "summary.json")


if __name__ == "__main__":
    unittest.main()
