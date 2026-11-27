"""Pin the Bandit gate: derived scope, explicit excludes, no baseline.

``app/bandit_gate.py`` scans non-excluded ``app/`` modules (derived via the
shared ``source_sweep.app_python_files`` helper, skipping virtualenvs and the
exclusions in ``app/bandit.ini``) plus the repo-root ``*.py`` scripts, and
fails when the JSON report still holds a medium-or-higher finding, any file
Bandit could not parse or read, any blanket ``nosec`` suppression, any dropped
target, or any ``nosec`` comment that does not name a single known Bandit test
id. These tests run in the Bandit-free test job without requiring bandit to be
installed.
"""

import configparser
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tokenize
import unittest
from unittest import mock

import bandit_gate
import source_sweep
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


def check_nosec_convention(path, valid_ids):
    """Return (marked_count, violations) for nosec comments in ``path``."""
    violations = []
    marked = 0
    with tokenize.open(path) as handle:
        source_text = handle.read()
    lines = source_text.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source_text).readline):
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string
        if not bandit_gate.NOSEC_ANY_CASE.search(comment):
            continue
        line_number = token.start[0]
        match = bandit_gate.VALID_NOSEC_LINE.match(comment)
        if not match:
            violations.append(
                f"{path.name}:{line_number} non-conforming nosec: {comment}")
            continue
        test_id = match.group("id")
        if test_id not in valid_ids:
            violations.append(
                f"{path.name}:{line_number} unknown bandit id: {test_id}")
            continue
        if line_number <= 1 or not lines[line_number - 2].strip().startswith("# "):
            violations.append(
                f"{path.name}:{line_number} missing reason comment on line above")
            continue
        marked += 1
    return marked, violations


class BanditGateTests(unittest.TestCase):
    def test_targets_are_derived_from_the_filesystem(self):
        self.assertIsNone(read_ini_option("targets"))
        included, excluded = bandit_gate.collect_targets()
        self.assertTrue(included)
        self.assertTrue(excluded)
        self.assertIn("app/bandit_gate.py", included)
        self.assertIn("app/source_sweep.py", included)
        self.assertIn("alexandria_compare.py", included)
        self.assertIn("app/tests/test_bandit_gate.py", excluded)
        self.assertNotIn("app/tests/test_bandit_gate.py", included)
        for target in (*included, *excluded):
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
            inc, exc = bandit_gate.collect_targets(root)
            self.assertEqual(["a_script.py", "app/new_module.py"], inc)
            self.assertEqual(["app/tests/test_x.py"], exc)
            self.assertEqual(inc, bandit_gate.get_targets(root))

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

    def test_targets_share_source_sweep_helper(self):
        self.assertIs(bandit_gate.app_python_files, source_sweep.app_python_files)
        sentinel = [Path("sentinel.py")]
        with mock.patch.object(source_sweep, "app_python_files",
                               return_value=sentinel) as shared:
            self.assertEqual(sentinel, app_python_files(ROOT / "app"))
        shared.assert_called_once_with(ROOT / "app")

    def test_command_stays_within_windows_argv_limit(self):
        targets = bandit_gate.get_targets()
        cmd = bandit_gate.get_command(targets)
        cmd_len = len(subprocess.list2cmdline(cmd))
        self.assertLess(cmd_len, bandit_gate.WINDOWS_CREATEPROCESS_LIMIT)
        self.assertLessEqual(cmd_len, bandit_gate.MAX_COMMAND_LINE_CHARS)

        # Even if the target set grows beyond MAX_COMMAND_LINE_CHARS, batching
        # keeps every invocation within budget without dropping any target.
        synthetic = [f"app/module_{index:04d}.py" for index in range(120)]
        max_chars = 600
        batches = bandit_gate.batch_targets(synthetic, max_chars=max_chars)
        self.assertGreater(len(batches), 1)
        flattened = [item for batch in batches for item in batch]
        self.assertEqual(synthetic, flattened)
        for batch in batches:
            batch_len = len(subprocess.list2cmdline(bandit_gate.get_command(batch)))
            self.assertLessEqual(batch_len, max_chars)

        merged = bandit_gate.merge_reports([
            {
                "errors": [],
                "generated_at": "t1",
                "results": [],
                "metrics": {
                    "_totals": {"SEVERITY.LOW": 3, "loc": 100, "nosec": 0, "skipped_tests": 1},
                    "app/a.py": {"loc": 100},
                },
            },
            {
                "errors": [{"filename": "app/bad.py", "reason": "syntax error"}],
                "generated_at": "t2",
                "results": [{"filename": "app/b.py", "line_number": 2,
                             "test_id": "B602", "issue_severity": "HIGH",
                             "issue_text": "shell=True"}],
                "metrics": {
                    "_totals": {"SEVERITY.LOW": 2, "loc": 50, "nosec": 1, "skipped_tests": 2},
                    "app/b.py": {"loc": 50},
                },
            },
        ])
        self.assertEqual(1, len(merged["errors"]))
        self.assertEqual(1, len(merged["results"]))
        self.assertEqual(5, merged["metrics"]["_totals"]["SEVERITY.LOW"])
        self.assertEqual(150, merged["metrics"]["_totals"]["loc"])
        self.assertEqual(1, merged["metrics"]["_totals"]["nosec"])
        self.assertEqual(3, merged["metrics"]["_totals"]["skipped_tests"])
        self.assertIn("app/a.py", merged["metrics"])
        self.assertIn("app/b.py", merged["metrics"])

    def test_excludes_match_bandit_walked_paths(self):
        value = read_ini_option("exclude")
        self.assertIsNotNone(value)
        patterns = bandit_gate.get_exclude_patterns()
        self.assertEqual(EXPECTED_EXCLUDES, patterns)
        self.assertEqual(EXPECTED_EXCLUDES, value.split(","))
        for path in ("app/tests/test_x.py",
                     "app/tests/nested/test_x.py",
                     "app/experiments/run.py",
                     "tools/ci/helper.py",
                     "app/benchmark_runner.py",
                     "app/run_isolated_api_tests.py"):
            with self.subTest(path=path):
                self.assertTrue(bandit_gate.is_excluded(path, patterns))
        for path in ("app\\tests\\test_x.py",
                     "app\\tests\\nested\\test_x.py",
                     "app\\experiments\\run.py",
                     "tools\\ci\\helper.py",
                     "app\\benchmark_runner.py",
                     "app\\run_isolated_api_tests.py"):
            with self.subTest(path=path):
                self.assertTrue(bandit_gate.is_excluded(path, patterns))
        for path in ("app/utils.py",
                     "app/hf_utils.py",
                     "app/routers/script.py",
                     "app/new_module.py",
                     "new_root_script.py",
                     "./new_root_script.py"):
            with self.subTest(path=path):
                self.assertFalse(bandit_gate.is_excluded(path, patterns))

        with tempfile.TemporaryDirectory() as temporary:
            bad_ini = Path(temporary) / "bandit.ini"
            bad_ini.write_text("[bandit]\nexclude = a\n[bandit]\nexclude = b\n",
                               encoding="utf-8")
            with self.assertRaises(configparser.DuplicateSectionError):
                bandit_gate.get_exclude_patterns(bad_ini)

    def test_missing_or_uninterpolatable_excludes_fail_the_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_option = root / "missing_option.ini"
            missing_option.write_text("[bandit]\n", encoding="utf-8")
            with self.assertRaises(configparser.Error):
                bandit_gate.get_exclude_patterns(missing_option)
            missing_section = root / "missing_section.ini"
            missing_section.write_text("[other]\nkey = 1\n", encoding="utf-8")
            with self.assertRaises(configparser.Error):
                bandit_gate.get_exclude_patterns(missing_section)
            # ConfigParser keeps its default interpolation, so a bare "%" raises
            # instead of being passed through to a pattern that cannot match.
            percent = root / "percent.ini"
            percent.write_text("[bandit]\nexclude = app/100%/x.py\n",
                               encoding="utf-8")
            with self.assertRaises(configparser.Error):
                bandit_gate.get_exclude_patterns(percent)

            stderr = io.StringIO()
            with mock.patch.object(
                    bandit_gate, "collect_targets",
                    side_effect=configparser.NoSectionError("bandit")), \
                    mock.patch.object(bandit_gate.subprocess, "run") as run_mock, \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(2, bandit_gate.main())
            run_mock.assert_not_called()
            self.assertIn("bandit did not run", stderr.getvalue())

    def test_workflow_runs_the_gate_on_the_linux_pin(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("python bandit_gate.py", workflow)
        self.assertIn(
            "python -m unittest -v tests.test_bandit_gate.BanditGateTests."
            "test_known_bandit_ids_covers_repo_and_matches_installed_bandit",
            workflow)
        self.assertNotIn("bandit-baseline", workflow)
        self.assertNotIn("--baseline", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertFalse((ROOT / "app" / "bandit-baseline.json").exists())

    def test_command_pins_the_full_bandit_argv(self):
        command = bandit_gate.get_command(["app/x.py", "probe_root.py"])
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "bandit"], command[1:3])
        self.assertNotIn("-r", command)
        self.assertIn("--exit-zero", command)
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

    def test_gate_fails_on_findings_scan_errors_blanket_nosec_and_dropped_targets(self):
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

        prefixed = bandit_gate.get_problems({
            "errors": [{"filename": "./app/bad.py", "reason": "syntax error"}],
            "results": [{"filename": "./app/new_module.py", "line_number": 4,
                         "test_id": "B602", "issue_severity": "HIGH",
                         "issue_text": "subprocess call with shell=True"}],
        })
        self.assertEqual(
            ["could not scan app/bad.py: syntax error",
             "app/new_module.py:4 B602 HIGH: subprocess call with shell=True"],
            prefixed)

        blanket = bandit_gate.get_problems({
            "errors": [],
            "results": [],
            "metrics": {"_totals": {"nosec": 1, "SEVERITY.LOW": 0}},
        })
        self.assertEqual(1, len(blanket))
        self.assertIn("blanket nosec", blanket[0])

        empty_targets = bandit_gate.get_problems(
            {"errors": [], "results": [], "metrics": {"_totals": {}}},
            expected_targets=[],
        )
        self.assertEqual(1, len(empty_targets))
        self.assertIn("empty", empty_targets[0])

        dropped = bandit_gate.get_problems(
            {
                "errors": [],
                "results": [],
                "metrics": {"_totals": {"nosec": 0}, "app/a.py": {}},
            },
            expected_targets=["app/a.py", "app/b.py"],
        )
        self.assertEqual(["target not reported by bandit: app/b.py"], dropped)

        errored_only = bandit_gate.get_problems(
            {
                "errors": [{"filename": "app/a.py", "reason": "syntax error"}],
                "results": [],
                "metrics": {"_totals": {"nosec": 0}},
            },
            expected_targets=["app/a.py"],
        )
        self.assertEqual(1, len(errored_only))
        self.assertIn("could not scan app/a.py", errored_only[0])

    def test_gate_rejects_unknown_nosec_id_and_non_conforming_comments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unknown_id.py").write_text(
                "# reviewed: typo\nx = 1  # nosec B999\n", encoding="utf-8")
            (root / "zero_id.py").write_text(
                "# reviewed: typo\nx = 1  # nosec B0\n", encoding="utf-8")
            (root / "bare.py").write_text(
                "# reviewed: bare\nx = 1  # nosec\n", encoding="utf-8")
            (root / "nospace.py").write_text(
                "# reviewed: no space\nx = 1  #nosec B314\n", encoding="utf-8")
            (root / "no_separator.py").write_text(
                "# reviewed: no separator\nx = 1  # nosecB314\n",
                encoding="utf-8")
            (root / "no_separator_no_space.py").write_text(
                "# reviewed: no separator or space\nx = 1  #nosecB314\n",
                encoding="utf-8")
            (root / "no_separator_two_ids.py").write_text(
                "# reviewed: bandit keeps only the last id\n"
                "x = 1  # nosecB314B602\n", encoding="utf-8")
            (root / "uppercase.py").write_text(
                "# reviewed: case typo\nx = 1  # NOSEC B314\n",
                encoding="utf-8")
            (root / "tabbed.py").write_text(
                "# reviewed: tab\nx = 1  #\tnosec\tB314\n", encoding="utf-8")
            (root / "colon.py").write_text(
                "# reviewed: colon\nx = 1  # nosec: B314\n", encoding="utf-8")
            (root / "other_id.py").write_text(
                "x = 1  # nosec B314 - also hides B602\n", encoding="utf-8")
            (root / "plugin_name.py").write_text(
                "x = 1  # nosec B314 - exec_used\n", encoding="utf-8")
            (root / "clean.py").write_text(
                "# reviewed: bounded input\nx = 1  # nosec B314\n",
                encoding="utf-8")
            (root / "builtin_id.py").write_text(
                "# reviewed: B001 is Bandit's builtin id\n"
                "x = 1  # nosec B001\n", encoding="utf-8")
            (root / "string_only.py").write_text(
                'EXAMPLE = "x = 1  # nosec B999"\n', encoding="utf-8")

            unknown = bandit_gate.get_nosec_problems(["unknown_id.py"], root)
            self.assertEqual(1, len(unknown))
            self.assertIn("unknown_id.py:2", unknown[0])
            self.assertIn("B999", unknown[0])

            zero = bandit_gate.get_nosec_problems(["zero_id.py"], root)
            self.assertEqual(1, len(zero))
            self.assertIn("B0", zero[0])

            for name in ("bare.py", "nospace.py", "no_separator.py",
                         "no_separator_no_space.py",
                         "no_separator_two_ids.py", "uppercase.py",
                         "tabbed.py", "colon.py",
                         "other_id.py", "plugin_name.py"):
                with self.subTest(name=name):
                    found = bandit_gate.get_nosec_problems([name], root)
                    self.assertEqual(1, len(found))
                    self.assertIn(name, found[0])

            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["clean.py"], root))
            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["builtin_id.py"], root))
            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["string_only.py"], root))

            # The convention helper must also reject both unknown ids and
            # #nosec without a space (with or without a reason line above).
            valid_ids = bandit_gate.get_known_test_ids()
            _, unknown_violations = check_nosec_convention(
                root / "unknown_id.py", valid_ids)
            self.assertEqual(1, len(unknown_violations))
            _, nospace_violations = check_nosec_convention(
                root / "nospace.py", valid_ids)
            self.assertEqual(1, len(nospace_violations))
            (root / "nospace_no_reason.py").write_text(
                "x = 1  #nosec B314\n", encoding="utf-8")
            _, nospace_no_reason_violations = check_nosec_convention(
                root / "nospace_no_reason.py", valid_ids)
            self.assertEqual(1, len(nospace_no_reason_violations))

            # Every spelling Bandit honours as a per-id suppression without the
            # exact "# nosec BXXX" shape must fail the convention helper too.
            for name in ("no_separator.py", "no_separator_no_space.py",
                         "no_separator_two_ids.py", "uppercase.py"):
                with self.subTest(convention=name):
                    _, violations = check_nosec_convention(
                        root / name, valid_ids)
                    self.assertEqual(1, len(violations))
                    self.assertIn(name, violations[0])

    def test_nosec_regex_matches_bandit_without_a_word_boundary(self):
        # bandit.core.manager.NOSEC_COMMENT is r"#\s*nosec:?\s*..." with no
        # word boundary, and _parse_nosec_comment turns "# nosecB314" into
        # {'B314'} (the last id wins). A \b in the gate let those spellings
        # bypass both the id check and the reason-on-the-line-above rule.
        for comment in ("# nosec B314", "# nosecB314", "#nosecB314",
                        "# nosec:B314", "# nosec_B314", "# nosecB314B602"):
            with self.subTest(comment=comment):
                self.assertTrue(bandit_gate.NOSEC_LINE.search(comment))
                self.assertTrue(bandit_gate.NOSEC_ANY_CASE.search(comment))
        # Bandit's regex is case-sensitive, so "# NOSEC B314" suppresses
        # nothing; the gate still rejects it as a non-conforming spelling.
        self.assertIsNone(bandit_gate.NOSEC_LINE.search("# NOSEC B314"))
        self.assertTrue(bandit_gate.NOSEC_ANY_CASE.search("# NOSEC B314"))

    def test_gate_reads_pep263_latin1_files_for_nosec_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_unknown = (
                "# -*- coding: latin-1 -*-\n"
                "# caf\u00e9\n"
                "x = 1  # nosec B999\n"
            ).encode("latin-1")
            (root / "latin1_unknown.py").write_bytes(bad_unknown)

            bad_shape = (
                "# -*- coding: latin-1 -*-\n"
                "# caf\u00e9\n"
                "x = 1  #nosec B314\n"
            ).encode("latin-1")
            (root / "latin1_nospace.py").write_bytes(bad_shape)

            clean_latin1 = (
                "# -*- coding: latin-1 -*-\n"
                "# caf\u00e9\n"
                "x = 1  # nosec B314\n"
            ).encode("latin-1")
            (root / "latin1_clean.py").write_bytes(clean_latin1)

            unknown = bandit_gate.get_nosec_problems(["latin1_unknown.py"], root)
            self.assertEqual(1, len(unknown))
            self.assertIn("latin1_unknown.py:3", unknown[0])
            self.assertIn("B999", unknown[0])

            nospace = bandit_gate.get_nosec_problems(["latin1_nospace.py"], root)
            self.assertEqual(1, len(nospace))
            self.assertIn("latin1_nospace.py:3", nospace[0])

            self.assertEqual(
                [], bandit_gate.get_nosec_problems(["latin1_clean.py"], root))

    def test_known_bandit_ids_covers_repo_and_matches_installed_bandit(self):
        self.assertIn("B001", bandit_gate.KNOWN_BANDIT_IDS)
        for test_id in ("B104", "B108", "B113", "B301", "B314", "B602", "B615"):
            self.assertIn(test_id, bandit_gate.KNOWN_BANDIT_IDS)
        self.assertNotIn("B999", bandit_gate.KNOWN_BANDIT_IDS)
        self.assertNotIn("B0", bandit_gate.KNOWN_BANDIT_IDS)
        if importlib.util.find_spec("bandit") is not None:
            installed = bandit_gate.get_known_test_ids(require_bandit=True)
            self.assertEqual(
                bandit_gate.KNOWN_BANDIT_IDS,
                installed,
            )
            self.assertIn("B001", installed)

    def test_low_findings_do_not_gate(self):
        self.assertEqual("medium", bandit_gate.SEVERITY_LEVEL)
        # LOW findings survive only as counted metrics; they are not in
        # results because bandit's severity filter removes them.
        report = {"errors": [], "results": [],
                  "metrics": {"_totals": {"SEVERITY.LOW": 253, "nosec": 0}}}
        self.assertEqual([], bandit_gate.get_problems(report))
        # The gate lists whatever reaches results; it never gates on LOW
        # metrics, so a LOW entry that did reach results would still be visible.
        low = {"filename": "app/new_module.py", "line_number": 3,
               "test_id": "B101", "issue_severity": "LOW",
               "issue_text": "assert_used"}
        self.assertEqual(1, len(bandit_gate.get_problems({"results": [low]})))

    def _main_patches(self, report_path, targets=("app/x.py",),
                      excluded=("app/tests/test_x.py",)):
        return (
            mock.patch.object(bandit_gate, "REPORT_PATH", report_path),
            mock.patch.object(bandit_gate, "collect_targets",
                              return_value=(list(targets), list(excluded))),
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

    def test_main_exits_2_when_targets_are_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report_patch, targets_patch, nosec_patch = self._main_patches(
                report, targets=(), excluded=())
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run") as run_mock:
                self.assertEqual(2, bandit_gate.main())
                run_mock.assert_not_called()

    def test_main_prints_the_scanned_file_count(self):
        report_data = {
            "errors": [],
            "results": [],
            "metrics": {
                "_totals": {"SEVERITY.LOW": 2, "nosec": 0},
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
                report,
                targets=("app/module.py", "root_script.py"),
                excluded=("app/tests/test_x.py",),
            )
            stdout = io.StringIO()
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      side_effect=write_clean_report), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(0, bandit_gate.main())
            self.assertIn(
                "bandit scanned 2 of 3 python files: 1 app files, "
                "1 root scripts (1 excluded)",
                stdout.getvalue())

    def test_main_runs_multiple_batches_and_writes_merged_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            calls = []

            def write_batch_report(command, cwd=None):
                calls.append(command)
                out_path = Path(command[command.index("-o") + 1])
                target = command[-1]
                data = {
                    "errors": [],
                    "results": [],
                    "metrics": {
                        "_totals": {"SEVERITY.LOW": 1, "nosec": 0, "loc": 10},
                        target: {"loc": 10},
                    },
                }
                out_path.write_text(json.dumps(data), encoding="utf-8")
                return mock.Mock(returncode=0)

            report_patch, targets_patch, nosec_patch = self._main_patches(
                report,
                targets=("app/a.py", "app/b.py"),
                excluded=("app/tests/test_x.py",),
            )
            with report_patch, targets_patch, nosec_patch, \
                    mock.patch.object(bandit_gate, "batch_targets",
                                      return_value=[["app/a.py"], ["app/b.py"]]), \
                    mock.patch.object(bandit_gate.subprocess, "run",
                                      side_effect=write_batch_report):
                self.assertEqual(0, bandit_gate.main())

            self.assertEqual(2, len(calls))
            merged = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(2, merged["metrics"]["_totals"]["SEVERITY.LOW"])
            self.assertEqual(20, merged["metrics"]["_totals"]["loc"])
            self.assertIn("app/a.py", merged["metrics"])
            self.assertIn("app/b.py", merged["metrics"])

    def test_nosec_comments_name_a_test_and_a_reason(self):
        valid_ids = bandit_gate.get_known_test_ids()
        production = [ROOT / rel for rel in bandit_gate.get_targets()]
        marked = 0
        all_violations = []
        for path in production:
            count, violations = check_nosec_convention(path, valid_ids)
            marked += count
            all_violations.extend(violations)
        self.assertEqual([], all_violations)
        self.assertTrue(marked)

    def test_report_artifact_is_not_tracked(self):
        gitignore = (ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("app/bandit-report.json", gitignore)
