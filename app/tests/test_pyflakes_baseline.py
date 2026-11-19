"""The pyflakes baseline gate.

The first test is the CI gate itself. The rest validate the instrument on
fixture trees (Rule 21): adding, removing, moving and duplicating a finding
must each produce the verdict they claim. No real app module is ever edited.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pyflakes_baseline import (APP_DIR, BASELINE_PATH, collect_findings,
                               write_baseline)

SCRIPT = APP_DIR / "pyflakes_baseline.py"


def make_tree(root, files):
    """Build a disposable ``app/`` tree; the gate scans only this shape."""
    app = root / "app"
    (app / "routers").mkdir(parents=True)
    for name, source in files.items():
        (app / name).write_text(source, encoding="utf-8")
    return app


def record_baseline(app, baseline):
    return write_baseline(baseline, collect_findings(app))


def run_check(app, baseline):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--check",
         "--app-dir", str(app), "--baseline", str(baseline)],
        cwd=APP_DIR, capture_output=True, text=True, timeout=60)


class PyflakesBaselineTests(unittest.TestCase):
    def test_current_tree_matches_the_checked_in_baseline(self):
        """The gate that runs in CI: a new finding anywhere in app fails it."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=APP_DIR, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode,
                         "New pyflakes findings:\n" + result.stdout + result.stderr)
        self.assertTrue(BASELINE_PATH.exists(), "the baseline must be checked in")

    def test_new_finding_fails_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_tree(root, {"mod.py": ""})
            baseline = root / "baseline.json"
            record_baseline(app, baseline)
            (app / "mod.py").write_text("import os\n", encoding="utf-8")
            result = run_check(app, baseline)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("app/mod.py: 'os' imported but unused", result.stdout)
            self.assertIn("NEW pyflakes findings", result.stdout)

    def test_removed_finding_is_stale_and_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_tree(root, {"mod.py": "import os\n"})
            baseline = root / "baseline.json"
            record_baseline(app, baseline)
            (app / "mod.py").write_text("# the import was removed\n", encoding="utf-8")
            result = run_check(app, baseline)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Stale baseline entries", result.stdout)
            self.assertNotIn("NEW pyflakes findings", result.stdout)

    def test_moving_a_finding_above_it_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_tree(root, {"mod.py": "import os\n"})
            baseline = root / "baseline.json"
            record_baseline(app, baseline)
            (app / "mod.py").write_text(
                "\n\n# an unrelated edit above the import\nimport os\n",
                encoding="utf-8")
            result = run_check(app, baseline)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("NEW pyflakes findings", result.stdout)

    def test_second_copy_of_a_baselined_finding_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_tree(root, {"mod.py": "def f():\n    import os\n"})
            baseline = root / "baseline.json"
            record_baseline(app, baseline)
            (app / "mod.py").write_text(
                "def f():\n    import os\n\n\ndef g():\n    import os\n",
                encoding="utf-8")
            result = run_check(app, baseline)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            # The baseline holds one copy, so the excess is one even though the
            # file now has two: a set-based gate would have passed this.
            self.assertIn("app/mod.py: 'os' imported but unused (x1)", result.stdout)

    def test_embedded_line_numbers_are_normalised(self):
        """A redefinition message contains 'from line N'; that N is not identity."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_tree(root / "a", {"mod.py": "import os\nimport os\n"})
            second = make_tree(root / "b", {"mod.py": "\n\nimport os\nimport os\n"})
            first_findings = collect_findings(first)
            self.assertTrue(any("from line N" in key for key in first_findings))
            self.assertEqual(first_findings, collect_findings(second))
