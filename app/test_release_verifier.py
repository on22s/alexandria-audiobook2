import unittest
from pathlib import Path
import subprocess
import tempfile

import verify_release


class ReleaseVerifierTests(unittest.TestCase):
    def test_python_compilation_includes_nonignored_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("ignored.py\nenv/\n", encoding="utf-8")
            (repo / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            (repo / "staged.py").write_text("STAGED = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True)
            (repo / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
            (repo / "ignored.py").write_text("not valid python !\n", encoding="utf-8")
            (repo / "env").mkdir()
            (repo / "env" / "ignored_env.py").write_text("not valid python !\n", encoding="utf-8")

            paths = verify_release.get_python_paths(repo)

            self.assertEqual(
                [repo / "staged.py", repo / "tracked.py", repo / "untracked.py"], paths
            )
            verify_release.compile_python_files(repo)

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
