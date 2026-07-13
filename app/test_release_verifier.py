import unittest

import verify_release


class ReleaseVerifierTests(unittest.TestCase):
    def test_api_summary_accepts_exact_quick_and_full_results(self):
        self.assertEqual(
            (71, 0, 12, 83),
            verify_release.validate_api_summary(
                "RESULTS: 71 passed, 0 failed, 12 skipped  (total: 83)", False
            ),
        )
        self.assertEqual(
            (83, 0, 0, 83),
            verify_release.validate_api_summary(
                "RESULTS: 83 passed, 0 failed, 0 skipped  (total: 83)", True
            ),
        )

    def test_api_summary_rejects_unexpected_skips(self):
        with self.assertRaisesRegex(ValueError, "Unexpected full API result"):
            verify_release.validate_api_summary(
                "RESULTS: 82 passed, 0 failed, 1 skipped  (total: 83)", True
            )

    def test_api_summary_rejects_missing_summary(self):
        with self.assertRaisesRegex(ValueError, "parseable RESULTS"):
            verify_release.validate_api_summary("server exited early", False)

    def test_unittest_summary_rejects_skips_and_missing_success(self):
        verify_release.validate_unittest_output("Ran 87 tests in 1.0s\n\nOK\n")
        with self.assertRaisesRegex(ValueError, "2 skipped"):
            verify_release.validate_unittest_output(
                "Ran 87 tests in 1.0s\n\nOK (skipped=2)\n"
            )
        with self.assertRaisesRegex(ValueError, "successful summary"):
            verify_release.validate_unittest_output("process stopped")


if __name__ == "__main__":
    unittest.main()
