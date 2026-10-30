import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

from experiments import tts_endpoint_content_screen as screen


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_wav(path, tail, prefix=None, rate=24000):
    samples = (prefix if prefix is not None else [0] * 720) + list(tail)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))


def manifest_case():
    input_value = {
        "text": "It isn't—step back before the cables snap.",
        "speaker_id": "Ryan",
        "delivery_instruction": "Reply urgently.",
        "pause_after_ms": 0,
    }
    voice = {"id": "Ryan", "asset_sha256": "a" * 64}
    settings = {"model": "qwen", "revision": "r1"}
    return {
        "id": "warning",
        "input": input_value,
        "input_sha256": digest(input_value),
        "voice": voice,
        "voice_sha256": digest(voice),
        "settings": settings,
        "settings_sha256": digest(settings),
        "attempts": [
            {"attempt": 1, "take_id": "warning-take-1", "seed": 101,
             "audio": "one.wav",
             "primary_transcript": "It isn't step back before the cables snap."},
            {"attempt": 2, "take_id": "warning-take-2", "seed": 102,
             "audio": "two.wav",
             "primary_transcript": ("It isn't step back with a long strange inserted "
                                    "passage before the cables now."),
             "confirmation_transcript": ("It isn't step back with another strange inserted "
                                         "passage before the cables now.")},
        ],
    }


class TTSEndpointContentScreenTests(unittest.TestCase):
    def test_reports_acoustic_terminal_and_bounded_comparison_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [0] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1, "cases": [manifest_case()]}), encoding="utf-8")
            report = screen.build_content_report(manifest)
            self.assertEqual("measured_not_production_qualified", report["status"])
            self.assertEqual(2, report["counts"]["attempts"])
            self.assertEqual(0, report["counts"]["endpoint_suspicious"])
            self.assertEqual(0, report["counts"]["primary_terminal_missing"])
            self.assertEqual(1, report["counts"]["primary_terminal_mismatch"])
            self.assertEqual(1, report["counts"]["same_terminal_mismatch"])
            first, second = report["cases"][0]["attempts"]
            self.assertEqual("snap", first["expected_terminal_token"])
            self.assertEqual("snap", first["primary_terminal_token"])
            self.assertEqual("now", second["primary_terminal_token"])
            self.assertEqual("now", second["confirmation_terminal_token"])
            self.assertTrue(second["primary_material_regions"])
            self.assertEqual(1, report["counts"]["corroborated_content_regions"])
            self.assertEqual([], first["corroborated_content_regions"])
            self.assertTrue(second["corroborated_content_regions"])
            self.assertEqual(
                hashlib.sha256(manifest_case()["attempts"][1][
                    "primary_transcript"].encode()).hexdigest(),
                second["primary_transcript_sha256"])
            self.assertNotIn("primary_transcript", second)

    def test_empty_transcript_is_missing_evidence_not_wrong_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [0] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            case = manifest_case()
            case["attempts"][0]["primary_transcript"] = "..."
            case["attempts"][0]["confirmation_transcript"] = ""
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "cases": [case]}),
                                encoding="utf-8")
            report = screen.build_content_report(manifest)
            first = report["cases"][0]["attempts"][0]
            self.assertEqual(1, report["counts"]["primary_terminal_missing"])
            self.assertEqual(1, report["counts"]["primary_terminal_mismatch"])
            self.assertTrue(first["primary_terminal_missing"])
            self.assertFalse(first["primary_terminal_mismatch"])
            self.assertFalse(first["same_terminal_mismatch"])

    def test_loud_endpoint_and_threshold_validation_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [8000] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1, "cases": [manifest_case()]}), encoding="utf-8")
            report = screen.build_content_report(manifest)
            self.assertEqual(1, report["counts"]["endpoint_suspicious"])
            for threshold in (-121, 1, float("nan"), float("inf"), True):
                with self.subTest(threshold=threshold), self.assertRaises(
                        screen.EndpointScreenError):
                    screen.build_content_report(manifest, threshold)

    def test_exact_shared_extra_region_is_reported_as_corroborated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [0] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            case = manifest_case()
            shared = ("It isn't step back alpha beta gamma delta before "
                      "the cables snap.")
            case["attempts"][1]["primary_transcript"] = shared
            case["attempts"][1]["confirmation_transcript"] = shared
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "cases": [case]}),
                                encoding="utf-8")
            report = screen.build_content_report(manifest)
            second = report["cases"][0]["attempts"][1]
            self.assertEqual(1, report["counts"]["corroborated_content_regions"])
            self.assertEqual("insert", second["corroborated_content_regions"][0]["kind"])

    def test_transcripts_and_frozen_attempt_identity_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [0] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            manifest = root / "manifest.json"
            bad_values = []
            case = manifest_case()
            case["attempts"][0]["primary_transcript"] = None
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][0]["primary_transcript"] = "x\ud800"
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][0]["confirmation_transcript"] = 4
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][1]["seed"] = case["attempts"][0]["seed"]
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][1]["take_id"] = case["attempts"][0]["take_id"]
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][1]["audio"] = case["attempts"][0]["audio"]
            bad_values.append(case)
            case = manifest_case()
            case["input"]["text"] = " ".join(["word"] * 1024)
            case["input_sha256"] = digest(case["input"])
            case["attempts"][0]["primary_transcript"] = " ".join(["word"] * 1024)
            bad_values.append(case)
            case = manifest_case()
            case["input"]["text"] = "..."
            case["input_sha256"] = digest(case["input"])
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][0]["primary_transcript"] = " ".join(["word"] * 2049)
            bad_values.append(case)
            case = manifest_case()
            case["attempts"][0]["confirmation_transcript"] = " ".join(
                ["word"] * 2049)
            bad_values.append(case)
            for case in bad_values:
                manifest.write_text(json.dumps({"version": 1, "cases": [case]}),
                                    encoding="utf-8")
                with self.subTest(case=case), self.assertRaises(screen.EndpointScreenError):
                    screen.build_content_report(manifest)

    def test_cli_writes_once_and_keeps_raw_transcripts_out_of_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "one.wav", [0] * 120)
            write_wav(root / "two.wav", [0] * 119 + [1])
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1, "cases": [manifest_case()]}), encoding="utf-8")
            output = root / "report.json"
            command = [sys.executable, "-m", "experiments.tts_endpoint_content_screen",
                       "--manifest", str(manifest), "--out", str(output)]
            first = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(0, first.returncode, first.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("tts_endpoint_content_screen.py",
                             report["provenance"]["script"])
            self.assertEqual({"window_ms": screen.DECISION_WINDOW_MS,
                              "operator": ">", "threshold_dbfs": -40.0},
                             report["endpoint_rule"])
            self.assertNotIn("primary_transcript",
                             report["cases"][0]["attempts"][1])
            self.assertIn("bounded token-level comparison evidence",
                          report["transcript_content_policy"])
            before = output.read_bytes()
            second = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(0, second.returncode)
            self.assertEqual(before, output.read_bytes())

    def test_cli_help_survives_optimized_docstring_stripping(self):
        result = subprocess.run(
            [sys.executable, "-OO", "-m", "experiments.tts_endpoint_content_screen",
             "--help"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Measure acoustic and transcript ending evidence", result.stdout)


if __name__ == "__main__":
    unittest.main()
