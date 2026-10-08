import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class AuditOutputPathTests(unittest.TestCase):
    def test_actual_cli_writes_and_checks_bare_and_nested_output_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            script = root / "tools/audit/audit_experiment_artifacts.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(REPO / "tools/audit/audit_experiment_artifacts.py", script)
            env = dict(os.environ, PYTHONPATH=str(REPO / "app"))
            for output in ("audit.json", "./explicit.json", "nested/audit.json"):
                with self.subTest(output=output):
                    result = subprocess.run([sys.executable, str(script), "--out", output],
                                            cwd=root, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual([], json.loads((root / output).read_text())["artifacts"])
                    check = subprocess.run([sys.executable, str(script), "--out", output, "--check"],
                                           cwd=root, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, check.returncode, check.stdout + check.stderr)
