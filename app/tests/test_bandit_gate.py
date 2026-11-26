"""Pin the Bandit gate: derived scope, explicit excludes, no baseline.

``app/bandit_gate.py`` scans all of ``app/`` (minus virtualenvs) plus the
repo-root ``*.py`` scripts with the exclusions in ``app/bandit.ini``, and fails
when the JSON report still holds a medium-or-higher finding, any file Bandit
could not parse or read, or a nosec comment that names more than one test id.
These tests pin those properties without importing bandit: the required test
job never installs it.
"""

import contextlib
import fnmatch
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import bandit_gate
from tests.test_support import app_python_files

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


def normalise(path):
    """Posix separators, the form the gate passes to bandit on every OS."""
    return str(path).replace("\\", "/")


def is_excluded(path, patterns):
    """Mirror bandit's path exclusion: glob match or literal substring."""
    path = normalise(path)
    return (any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
            or any(pattern in path for pattern in patterns))


class BanditGateTests(unittest.TestCase):
    def test_targets_are_derived_from_the_filesystem(self):
        self.assertIsNone(read_ini_option("targets"))
        targets = bandit_gate.get_targets()
        self.assertIn("app/bandit_gate.py", targets)
        self.assertIn("alexandria_compare.py", targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertNotIn("\\", target)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "app" / "new_module.py").write_text("", encoding="utf-8")
            (root / "app" / "tests").mkdir()
            (root / "app" / "tests" / "test_x.py").write_text(
                "", encoding="utf-8")
            (root / "a_script.py").write_text("", encoding="utf-8")
            self.assertEqual(
                ["a_script.py", "app/new_module.py", "app/tests/test_x.py"],
                bandit_gate.get_targets(root))

    def test_targets_skip_virtualenvs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "app" / "env" / "lib" / "python3.10" / "site-packages"
            site.mkdir(parents=True)
            (root / "app" / "env" / "pyvenv.cfg").write_text(
                "home = /usr/bin\n", encoding="utf-8")
            (site / "third_party.py").write_text(
                "subprocess.run('x', shell=True)\n", encoding="utf-8")
            (root / "app" / "env" / "lib" / "python3.10" / "stdlib.py").write_text(
                "", encoding="utf-8")
            (root / "app" / "vendor" / "site-packages").mkdir(parents=True)
            (root / "app" / "vendor" / "site-packages" / "vendored.py").write_text(
                "", encoding="utf-8")
            (root / "app" / "second_env").mkdir()
            (root / "app" / "second_env" / "pyvenv.cfg").write_text(
                "", encoding="utf-8")
            (root / "app" / "second_env" / "local.py").write_text(
                "", encoding="utf-8")
            (root / "app" / "keep.py").write_text("", encoding="utf-8")
            (root / "root_script.py").write_text("", encoding="utf-8")

            targets = bandit_gate.get_targets(root)

            self.assertEqual(["app/keep.py", "root_script.py"], targets)
            for target in targets:
                self.assertNotIn("env", Path(target).parts)
                self.assertNotIn("site-packages", Path(target).parts)

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
        for path in ("app\\tests\\test_x.py",
                     "app\\tests\\nested\\test_x.py",
                     "app\\experiments\\run.py",
                     "tools\\ci\\helper.py",
                     "app\\benchmark_runner.py",
                     "app\\run_isolated_api_tests.py"):
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
        self.assertNotIn("continue-on-error", workflow)
        self.assertFalse((ROOT / "app" / "bandit-baseline.json").exists())

    def test_command_pins_the_full_bandit_argv(self):
        command = bandit_gate.get_command(["app/x.py", "probe_root.py"])
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "bandit"], command[1:3])
        self.assertIn("-r", command)
        self.assertIn("--exit-zero", command)
        self.assertEqual(1, command.count("-r"))
        self.assertEqual(1, command.count("--exit-zero"))
        ini_index = command.index("--ini")
        self.assertEqual(str(INI_PATH), command[ini_index + 1])
        format_index = command.index("-f")
        self.assertEqual("json", command[format_index + 1])
        output_index = command.index("-o")
        self.assertEqual(str(bandit_gate.REPORT_PATH),
                         command[output_index + 1])
        severity_index = command.index("--severity-level")
        self.assertEqual("medium", command[severity_index + 1])
        self.assertEqual(["app/x.py", "probe_root.py"], command[-2:])

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

    def test_gate_rejects_non_conforming_nosec_comments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "other_id.py").write_text(
                "x = 1  # nosec B314 - also hides B602\n", encoding="utf-8")
            (root / "plugin_name.py").write_text(
                "x = 1  # nosec B314 - exec_used\n", encoding="utf-8")
            (root / "clean.py").write_text(
                "# reviewed: bounded input\nx = 1  # nosec B314\n",
                encoding="utf-8")
            (root / "string_only.py").write_text(
                'EXAMPLE = "x = 1  # nosec B314 - also hides B602"\n',
                encoding="utf-8")

            other_id = bandit_gate.get_nosec_problems(["other_id.py"], root)
            self.assertEqual(1, len(other_id))
            self.assertIn("other_id.py:1", other_id[0])
            plugin = bandit_gate.get_nosec_problems(["plugin_name.py"], root)
            self.assertEqual(1, len(plugin))
            self.assertIn("plugin_name.py:1", plugin[0])
            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["clean.py"], root))
            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["string_only.py"], root))

    def test_low_findings_do_not_gate(self):
        self.assertEqual("medium", bandit_gate.SEVERITY_LEVEL)
        # LOW findings survive only as counted metrics; they are not in
        # results because bandit's severity filter removes them.
        report = {"errors": [], "results": [],
                  "metrics": {"_totals": {"SEVERITY.LOW": 253}}}
        self.assertEqual([], bandit_gate.get_problems(report))
        # The gate lists whatever reaches results; it never consults metrics,
        # so a LOW entry that did reach results would still be visible.
        low = {"filename": "app/new_module.py", "line_number": 3,
               "test_id": "B101", "issue_severity": "LOW",
               "issue_text": "assert_used"}
        self.assertEqual(1, len(bandit_gate.get_problems({"results": [low]})))

    def _main_patches(self, report_path, targets=("app/x.py",)):
        return (
            mock.patch.object(bandit_gate, "REPORT_PATH", report_path),
            mock.patch.object(bandit_gate, "get_targets",
                              return_value=list(targets)),
            mock.patch.object(bandit_gate, "get_nosec_problems",
                              return_value=[]),
        )

    def test_main_exits_2_when_bandit_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report_patch, targets_patch, nosec_patch = self._main_patches(
                report)
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      side_effect=FileNotFoundError("bandit")):
                self.assertEqual(2, bandit_gate.main())

    def test_main_exits_2_when_bandit_crashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report_patch, targets_patch, nosec_patch = self._main_patches(
                report)
            crash = mock.Mock(returncode=3)
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      return_value=crash):
                self.assertEqual(2, bandit_gate.main())

    def test_main_exits_2_when_report_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report_patch, targets_patch, nosec_patch = self._main_patches(
                report)
            completed = mock.Mock(returncode=0)
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      return_value=completed):
                self.assertEqual(2, bandit_gate.main())

    def test_main_exits_2_when_report_is_not_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"

            def write_bad_report(command, cwd=None):
                report.write_text("{not json", encoding="utf-8")
                return mock.Mock(returncode=0)

            report_patch, targets_patch, nosec_patch = self._main_patches(
                report)
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      side_effect=write_bad_report):
                self.assertEqual(2, bandit_gate.main())

    def test_main_prints_the_scanned_file_count(self):
        report_data = {
            "errors": [],
            "results": [],
            "metrics": {
                "_totals": {"SEVERITY.LOW": 2},
                "app/module.py": {},
                "./root_script.py": {},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"

            def write_clean_report(command, cwd=None):
                report.write_text(json.dumps(report_data), encoding="utf-8")
                return mock.Mock(returncode=0)

            report_patch, targets_patch, nosec_patch = self._main_patches(
                report)
            stdout = io.StringIO()
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      side_effect=write_clean_report), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(0, bandit_gate.main())
            self.assertIn(
                "bandit scanned 2 files: 1 app files, 1 root scripts",
                stdout.getvalue())

    def test_nosec_comments_name_a_test_and_a_reason(self):
        production = [path for path in
                      [*ROOT.glob("*.py"), *app_python_files(ROOT / "app")]
                      if not {"tests", "experiments", "tools"}
                      & set(path.relative_to(ROOT).parts)]
        marked = 0
        for path in production:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if "# nosec" not in line:
                    continue
                with self.subTest(file=path.name, line=number):
                    self.assertRegex(line, r"# nosec B\d+\s*$")
                    self.assertGreater(number, 1)
                    self.assertRegex(lines[number - 2].strip(), r"^# \S")
                marked += 1
        self.assertTrue(marked)

    def test_report_artifact_is_not_tracked(self):
        gitignore = (ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("app/bandit-report.json", gitignore)
