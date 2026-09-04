"""Negative controls for checks relied on by GOALS 6.6."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).parent.parent
REPO = APP.parent


class EmptyScopeRejectionTests(unittest.TestCase):
    def run_script(self, relative, *args):
        return subprocess.run(
            [sys.executable, str(REPO / relative), *map(str, args)],
            cwd=REPO, capture_output=True, text=True,
        )

    def test_source_encoding_audit_rejects_an_empty_input_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(
                "app/experiments/audit_source_encoding.py",
                "--inputs", tmp, "--out", Path(tmp) / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was audited", result.stderr + result.stdout)

    def test_tts_boundary_audit_rejects_an_empty_script_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(
                "app/experiments/tts_boundary_audit.py",
                "--scripts", tmp, "--out", Path(tmp) / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was audited", result.stderr + result.stdout)

    def test_production_trigram_audit_rejects_an_empty_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            sources = root / "sources"
            scripts.mkdir()
            sources.mkdir()
            result = self.run_script(
                "app/experiments/production_trigram_audit.py",
                "--scripts", scripts, "--sources", sources,
                "--out", root / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was audited", result.stderr + result.stdout)

    def test_pdnc_context_audit_rejects_an_empty_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(
                "app/experiments/pdnc_context_audit.py",
                "--pdnc", tmp, "--out", Path(tmp) / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was audited", result.stderr + result.stdout)

    def test_candidate_membership_audit_rejects_a_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(
                "app/experiments/audit_candidate_membership.py",
                Path(tmp) / "missing.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was audited", result.stderr + result.stdout)


class GoalEvidenceCheckTests(unittest.TestCase):
    def test_check_rejects_a_stale_goal_evidence_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals = root / "GOALS.md"
            audit = root / "structural.json"
            out = root / "goal.json"
            goals.write_text("### 1.1 First\n`probe.json`\n", encoding="utf-8")
            audit.write_text(json.dumps({"artifacts": [{
                "artifact": "probe.json", "classification": "provisional",
                "dirty": False, "commit": "abc123",
            }]}), encoding="utf-8")
            command = [
                sys.executable, str(REPO / "app/experiments/goal_evidence_audit.py"),
                "--goals", str(goals), "--audit", str(audit), "--out", str(out),
            ]
            created = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            goals.write_text("### 1.1 Changed\n`probe.json`\n", encoding="utf-8")
            checked = subprocess.run(
                command + ["--check"], cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("stale", checked.stderr + checked.stdout)

    def test_goal_evidence_audit_rejects_empty_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals = root / "GOALS.md"
            audit = root / "audit.json"
            goals.write_text("", encoding="utf-8")
            audit.write_text('{"artifacts": []}', encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(REPO / "app/experiments/goal_evidence_audit.py"),
                "--goals", str(goals), "--audit", str(audit),
                "--out", str(root / "out.json"),
            ], cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no goals found", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
