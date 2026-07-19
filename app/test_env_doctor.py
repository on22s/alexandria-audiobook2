import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env_doctor import evaluate_env


class EvaluateEnvTests(unittest.TestCase):
    def test_all_required_present_is_ok(self):
        spec = {"required": ["torch", "numpy"], "optional": [], "version_hint": {}}
        probe = {"torch": "2.10.0+rocm7.0", "numpy": "1.26.0"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        statuses = {r["package"]: r["status"] for r in rows}
        self.assertEqual({"torch": "OK", "numpy": "OK"}, statuses)

    def test_required_missing_fails(self):
        spec = {"required": ["torch", "peft"], "optional": [], "version_hint": {}}
        probe = {"torch": "2.10.0+rocm7.0", "peft": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertFalse(ok)
        peft_row = next(r for r in rows if r["package"] == "peft")
        self.assertEqual("MISSING", peft_row["status"])

    def test_optional_missing_passes_with_note(self):
        spec = {
            "required": ["torch"],
            "optional": ["pyannote.audio"],
            "version_hint": {},
        }
        probe = {"torch": "2.10.0+rocm7.0", "pyannote.audio": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        opt_row = next(r for r in rows if r["package"] == "pyannote.audio")
        self.assertEqual("OPTIONAL-MISSING", opt_row["status"])
        self.assertIn("diarization unavailable", opt_row["note"])

    def test_optional_missing_without_note_still_passes(self):
        spec = {"required": [], "optional": ["seaborn"], "version_hint": {}}
        probe = {"seaborn": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("OPTIONAL-MISSING", rows[0]["status"])
        self.assertEqual("", rows[0]["note"])

    def test_interpreter_not_found_fails_all_required(self):
        spec = {
            "required": ["torch", "librosa"],
            "optional": ["matplotlib"],
            "version_hint": {},
        }
        rows, ok = evaluate_env(spec, None)
        self.assertFalse(ok)
        required_rows = [r for r in rows if r["expected"] == "required"]
        self.assertTrue(all(r["status"] == "MISSING" for r in required_rows))
        optional_rows = [r for r in rows if r["expected"] == "optional"]
        self.assertTrue(all(r["status"] == "OPTIONAL-MISSING" for r in optional_rows))
        self.assertFalse(ok)

    def test_version_hint_mismatch_adds_note_but_still_ok(self):
        spec = {
            "required": ["torch"],
            "optional": [],
            "version_hint": {"torch": "2.10.0+rocm"},
        }
        probe = {"torch": "2.4.0+cu121"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("OK", rows[0]["status"])
        self.assertIn("expected version prefix", rows[0]["note"])

    def test_version_hint_match_has_no_note(self):
        spec = {
            "required": ["torch"],
            "optional": [],
            "version_hint": {"torch": "2.10.0+rocm"},
        }
        probe = {"torch": "2.10.0+rocm7.0"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("", rows[0]["note"])


if __name__ == "__main__":
    unittest.main()
