import json
import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

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

    def test_api_summary_uses_suite_inventory_for_quick_and_full_results(self):
        quick = self._api_summary("quick", "skipped")
        self.assertEqual(
            quick["counts"], verify_release.validate_api_summary(quick, False)
        )
        full = self._api_summary("full", "passed")
        self.assertEqual(
            full["counts"], verify_release.validate_api_summary(full, True)
        )

    def test_api_summary_rejects_wrong_skip_identity_with_same_counts(self):
        summary = self._api_summary("quick", "passed")
        summary["tests"][0]["status"] = "skipped"
        summary["counts"].update(passed=1, skipped=1)
        with self.assertRaisesRegex(ValueError, "Unexpected quick API skips"):
            verify_release.validate_api_summary(summary, False)

    def test_api_summary_rejects_inconsistent_counts_and_duplicate_names(self):
        summary = self._api_summary("quick", "skipped")
        summary["counts"]["passed"] = 2
        with self.assertRaisesRegex(ValueError, "do not match test records"):
            verify_release.validate_api_summary(summary, False)

        summary = self._api_summary("quick", "skipped")
        summary["tests"][1]["name"] = summary["tests"][0]["name"]
        with self.assertRaisesRegex(ValueError, "non-empty and unique"):
            verify_release.validate_api_summary(summary, False)

    @staticmethod
    def _api_summary(mode, full_only_status):
        tests = [
            {"name": "always", "requires_full": False, "status": "passed"},
            {"name": "gpu", "requires_full": True, "status": full_only_status},
        ]
        if full_only_status == "skipped":
            tests[1]["reason"] = "requires --full"
        passed = sum(test["status"] == "passed" for test in tests)
        skipped = sum(test["status"] == "skipped" for test in tests)
        return {
            "schema_version": 1, "mode": mode,
            "counts": {"passed": passed, "failed": 0, "skipped": skipped, "total": 2},
            "tests": tests,
        }

    def test_unittest_summary_rejects_skips_and_missing_success(self):
        verify_release.validate_unittest_output("Ran 87 tests in 1.0s\n\nOK\n")
        with self.assertRaisesRegex(ValueError, "2 skipped"):
            verify_release.validate_unittest_output(
                "Ran 87 tests in 1.0s\n\nOK (skipped=2)\n"
            )
        with self.assertRaisesRegex(ValueError, "successful summary"):
            verify_release.validate_unittest_output("process stopped")

    def test_json_report_records_successful_gates_and_api_results(self):
        api_result = {
            "counts": {"passed": 1, "failed": 0, "skipped": 1, "total": 2},
            "skips": [{"name": "gpu", "reason": "requires --full"}],
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(verify_release, "compile_python_files", return_value=None), \
             patch.object(verify_release, "run_command"), \
             patch.object(verify_release, "run_api_suite", return_value=api_result):
            report_path = Path(tmp) / "report.json"
            self.assertEqual(0, verify_release.main(["--json-report", str(report_path)]))

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("passed", report["status"])
        self.assertEqual("quick", report["mode"])
        self.assertEqual(
            ["compile_python", "unit_tests", "api_contract", "api_tests"],
            [gate["name"] for gate in report["gates"]],
        )
        self.assertEqual(api_result, report["gates"][-1]["result"])
        self.assertTrue(all(gate["status"] == "passed" for gate in report["gates"]))

    def test_json_report_is_written_on_failure_with_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(
                 verify_release, "compile_python_files",
                 side_effect=ValueError("token=do-not-store compilation failed"),
             ):
            report_path = Path(tmp) / "report.json"
            self.assertEqual(1, verify_release.main(["--json-report", str(report_path)]))

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("failed", report["status"])
        self.assertEqual("compile_python", report["failure"]["gate"])
        self.assertEqual("token=[REDACTED] compilation failed", report["failure"]["message"])
        self.assertNotIn("do-not-store", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
