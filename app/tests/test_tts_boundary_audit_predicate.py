"""#2118: the boundary audit must speak with the runtime's one voice.

The experiment used to declare its own UNSPEAKABLE_CATEGORIES, which omitted
Cc. The runtime sanitizer strips C0/C1 controls, so the instrument reported
fewer characters removed than the sanitizer actually removed. These tests pin
the audit's classification to the canonical speech predicate and check both
sides of the instrument: it rejects real controls and accepts ordinary speech.

All text is synthetic; no saved book, runtime config or TTS engine is used.
"""
import json
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

from speech_policy import UNSPEAKABLE_CATEGORIES, is_unspeakable_character
from experiments.tts_boundary_audit import audit_text, is_unspeakable

APP = Path(__file__).parent.parent
REPO = APP.parent
AUDIT_SCRIPT = REPO / "app" / "experiments" / "tts_boundary_audit.py"


class PredicateParityTests(unittest.TestCase):
    """One witness per category must be classified identically by both."""

    CATEGORY_SAMPLES = {
        "So": "\u2603",   # snowman
        "Sm": "\u2192",   # rightwards arrow
        "Sk": "\u00b4",   # acute accent
        "Co": "\ue000",   # private use
        "Cn": "\u0378",   # unassigned
        "Cs": "\ud800",   # high surrogate
        "Cc": "\x1b",     # ESC
    }

    def test_every_category_has_a_sample_in_the_runtime_set(self):
        self.assertEqual(set(self.CATEGORY_SAMPLES), set(UNSPEAKABLE_CATEGORIES))
        for category, char in self.CATEGORY_SAMPLES.items():
            with self.subTest(category=category):
                self.assertEqual(category, unicodedata.category(char))

    def test_audit_predicate_matches_runtime_on_every_category(self):
        for category, char in self.CATEGORY_SAMPLES.items():
            with self.subTest(category=category):
                self.assertEqual(is_unspeakable_character(char), is_unspeakable(char))
                self.assertTrue(is_unspeakable(char))

    def test_c0_controls_the_runtime_removes_are_unspeakable_to_the_audit(self):
        for char in ("\x07", "\x01", "\x1b"):  # BEL, SOH, ESC
            with self.subTest(codepoint=ord(char)):
                self.assertTrue(is_unspeakable_character(char))
                self.assertTrue(is_unspeakable(char))

    def test_exemptions_are_classified_identically(self):
        # Whitespace (including control whitespace), currency and letters are
        # deliberately not unspeakable; both predicates must agree on that.
        for char in (" ", "\t", "\n", "$", "\u00e9", "A"):
            with self.subTest(codepoint=ord(char)):
                self.assertFalse(is_unspeakable_character(char))
                self.assertFalse(is_unspeakable(char))


class AuditInstrumentTests(unittest.TestCase):
    """The audit itself, not just the predicate, must see the controls."""

    def test_audit_text_rejects_a_c0_control(self):
        counts = audit_text("Hello\x07World")
        self.assertEqual(1, counts["\x07"])
        self.assertEqual(1, sum(counts.values()))

    def test_audit_text_accepts_normal_text(self):
        self.assertEqual(0, sum(audit_text("Hello, world. \u00e9\u6c49\u5b57 $5").values()))

    def test_script_entrypoint_counts_a_control_the_runtime_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "book.json").write_text(json.dumps([
                {"text": "Hello\x07\x84World"},
            ]), encoding="utf-8")
            out = root / "out.json"
            result = subprocess.run(
                [sys.executable, str(AUDIT_SCRIPT),
                 "--scripts", str(root), "--out", str(out)],
                cwd=REPO, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["characters_before_normalization"])
            self.assertEqual(2, payload["removed_by_normalization"])
            self.assertEqual(0, payload["characters_at_tts_boundary"])


if __name__ == "__main__":
    unittest.main()
