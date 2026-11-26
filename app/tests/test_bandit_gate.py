"""Pin the Bandit gate: derived scope, explicit excludes, no baseline.

``app/bandit_gate.py`` scans all of ``app/`` plus the repo-root ``*.py``
scripts with the exclusions in ``app/bandit.ini``, and fails when the JSON
report still holds a medium-or-higher finding or any file Bandit could not
parse or read. These tests pin those properties without importing bandit:
the required test job never installs it.
"""

import fnmatch
from pathlib import Path
import tempfile
import unittest

import bandit_gate

ROOT = Path(__file__).resolve().parents[2]
INI_PATH = ROOT / "app" / "bandit.ini"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "bandit.yml"
EXPECTED_EXCLUDES = [
    "app/tests/*",
    "app/experiments/*",
    "tools/*",
    "app/benchmark_runner.py",
    "app/run_isolated_api_tests.py",
]


def read_ini_option(name):
    """Return the [bandit] option value from app/bandit.ini, or None."""
    section = None
    for line in INI_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != "bandit" or not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return None


def is_excluded(path, patterns):
    """Mirror bandit's path exclusion: glob match or literal substring."""
    return (any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
            or any(pattern in path for pattern in patterns))


class BanditGateTests(unittest.TestCase):
    def test_targets_are_derived_from_the_filesystem(self):
        self.assertIsNone(read_ini_option("targets"))
        self.assertEqual(["app", *sorted(path.name for path in ROOT.glob("*.py"))],
                         bandit_gate.get_targets())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "new_module.py").write_text("", encoding="utf-8")
            (root / "a_script.py").write_text("", encoding="utf-8")
            self.assertEqual(["app", "a_script.py", "new_module.py"],
                             bandit_gate.get_targets(root))

    def test_excludes_match_bandit_walked_paths(self):
        value = read_ini_option("exclude")
        self.assertIsNotNone(value)
        patterns = value.split(",")
        self.assertEqual(EXPECTED_EXCLUDES, patterns)
        for path in ("app/tests/test_x.py",
                     "app/tests/nested/test_x.py",
                     "app/experiments/run.py",
                     "tools/ci/helper.py",
                     "app/benchmark_runner.py",
                     "app/run_isolated_api_tests.py"):
            with self.subTest(path=path):
                self.assertTrue(is_excluded(path, patterns))
        for path in ("app/utils.py",
                     "app/hf_utils.py",
                     "app/routers/script.py",
                     "app/new_module.py",
                     "new_root_script.py",
                     "./new_root_script.py"):
            with self.subTest(path=path):
                self.assertFalse(is_excluded(path, patterns))

    def test_workflow_runs_the_gate_on_the_linux_pin(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("python bandit_gate.py", workflow)
        self.assertNotIn("bandit-baseline", workflow)
        self.assertNotIn("--baseline", workflow)
        self.assertFalse((ROOT / "app" / "bandit-baseline.json").exists())

    def test_gate_fails_on_findings_and_on_scan_errors(self):
        self.assertEqual([], bandit_gate.get_problems({"errors": [],
                                                       "results": []}))
        errors = bandit_gate.get_problems({
            "errors": [{"filename": "app/archive_utils.py",
                        "reason": "syntax error while parsing AST from file"}],
            "results": [],
        })
        self.assertEqual(1, len(errors))
        self.assertIn("app/archive_utils.py", errors[0])
        self.assertIn("syntax error", errors[0])
        findings = bandit_gate.get_problems({
            "errors": [],
            "results": [{"filename": "app/new_module.py", "line_number": 7,
                         "test_id": "B602", "issue_severity": "HIGH",
                         "issue_text": "subprocess call with shell=True"}],
        })
        self.assertEqual(1, len(findings))
        self.assertIn("B602", findings[0])

    def test_low_findings_do_not_gate(self):
        self.assertEqual("medium", bandit_gate.SEVERITY_LEVEL)

    def test_nosec_comments_name_a_test_and_a_reason(self):
        production = [path for path in
                      [*ROOT.glob("*.py"), *(ROOT / "app").rglob("*.py")]
                      if not {"tests", "experiments", "tools"}
                      & set(path.relative_to(ROOT).parts)]
        marked = []
        for path in production:
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "# nosec" not in line:
                    continue
                with self.subTest(file=path.name, line=number):
                    self.assertRegex(line, r"# nosec B\d+ - \S+")
                marked.append(path)
        self.assertTrue(marked)

    def test_report_artifact_is_not_tracked(self):
        gitignore = (ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("app/bandit-report.json", gitignore)
