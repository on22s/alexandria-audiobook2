import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

from experiments import tts_endpoint_retry_screen as retry


def write_wav(path, sample, rate=24000):
    samples = [0] * 720 + [sample] * 120
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))


def bound(value):
    import hashlib
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return value, hashlib.sha256(raw).hexdigest()


def case(case_id, first, second, labels=("cut", "clean"), seeds=(1, 2)):
    input_value, input_sha256 = bound({
        "text": "Will you wait?", "speaker_id": "speaker-1",
        "delivery_instruction": "quietly", "pause_after_ms": 250})
    voice, voice_sha256 = bound({"id": "voice-1", "asset_sha256": "4" * 64})
    settings, settings_sha256 = bound({"temperature": 0.9, "top_p": 1.0})
    return {
        "id": case_id,
        "input": input_value,
        "input_sha256": input_sha256,
        "voice": voice,
        "voice_sha256": voice_sha256,
        "settings": settings,
        "settings_sha256": settings_sha256,
        "attempts": [
            {"attempt": 1, "take_id": f"{case_id}-take-1", "seed": seeds[0],
             "label": labels[0], "audio": first},
            {"attempt": 2, "take_id": f"{case_id}-take-2", "seed": seeds[1],
             "label": labels[1], "audio": second},
        ],
    }


class TTSEndpointRetryScreenTests(unittest.TestCase):
    def test_policy_reports_first_retry_and_exhausted_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, sample in (("cut-1.wav", 12000), ("clean-1.wav", 0),
                                 ("clean-2.wav", 0), ("cut-2.wav", 12000),
                                 ("cut-3.wav", 12000), ("cut-4.wav", 12000)):
                write_wav(root / name, sample)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "cases": [
                case("retry", "cut-1.wav", "clean-1.wav"),
                case("first", "clean-2.wav", "cut-2.wav", labels=("clean", "cut")),
                case("exhausted", "cut-3.wav", "cut-4.wav", labels=("cut", "cut")),
            ]}), encoding="utf-8")

            report = retry.build_retry_report(manifest)

            self.assertEqual("simulated_not_production_qualified", report["status"])
            self.assertEqual({"accepted_first": 1, "accepted_retry": 1,
                              "exhausted": 1, "false_accept_cases": 0,
                              "false_reject_cases": 0,
                              "false_reject_attempts": 0},
                             report["policy_outcomes"])
            self.assertEqual([2, 1, None],
                             [row["accepted_attempt"] for row in report["cases"]])
            self.assertTrue(all(
                attempt["used_by_policy"]
                for attempt in report["cases"][0]["attempts"]))
            self.assertFalse(report["cases"][1]["attempts"][1]["used_by_policy"])
            self.assertEqual(
                "retry-take-1",
                report["cases"][1]["attempts"][1]["duplicate_audio_of_take_id"])
            self.assertTrue(all(
                attempt["used_by_policy"]
                for attempt in report["cases"][2]["attempts"]))
            self.assertEqual(4, report["all_attempt_confusion_matrix"]["true_positive"])
            self.assertEqual(2, report["all_attempt_confusion_matrix"]["true_negative"])

    def test_labels_remain_independent_of_threshold_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "quiet-1.wav", 0)
            write_wav(root / "loud-1.wav", 12000)
            write_wav(root / "quiet-2.wav", 0)
            write_wav(root / "loud-2.wav", 12000)
            write_wav(root / "loud-3.wav", 12000)
            write_wav(root / "quiet-3.wav", 0)
            write_wav(root / "loud-4.wav", 12000)
            write_wav(root / "loud-5.wav", 12000)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "cases": [
                case("false-accept", "quiet-1.wav", "loud-1.wav",
                     labels=("cut", "clean")),
                case("false-reject", "loud-2.wav", "quiet-2.wav",
                     labels=("clean", "clean")),
                case("retry-false-accept", "loud-3.wav", "quiet-3.wav",
                     labels=("cut", "cut")),
                case("double-false-reject", "loud-4.wav", "loud-5.wav",
                     labels=("clean", "clean")),
            ]}), encoding="utf-8")
            report = retry.build_retry_report(manifest)
            self.assertEqual(2, report["policy_outcomes"]["false_accept_cases"])
            self.assertEqual(2, report["policy_outcomes"]["false_reject_cases"])
            self.assertEqual(3, report["policy_outcomes"]["false_reject_attempts"])
            self.assertEqual(2, report["used_attempt_confusion_matrix"]["false_negative"])
            self.assertEqual(3, report["used_attempt_confusion_matrix"]["false_positive"])

    def test_malformed_identity_attempts_and_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inside"
            root.mkdir()
            write_wav(root / "audio-1.wav", 0)
            write_wav(root / "audio-2.wav", 0)
            write_wav(root / "audio-3.wav", 0)
            write_wav(root / "audio-4.wav", 0)
            manifest = root / "manifest.json"
            baseline = case("one", "audio-1.wav", "audio-2.wav")
            empty_settings = json.loads(json.dumps(baseline))
            empty_settings["settings"], empty_settings["settings_sha256"] = bound({})
            manifest.write_text(json.dumps({"version": 1, "cases": [empty_settings]}),
                                encoding="utf-8")
            self.assertEqual("accepted_first",
                             retry.build_retry_report(manifest)["cases"][0]["outcome"])
            variants = []
            bad_hash = json.loads(json.dumps(baseline))
            bad_hash["input_sha256"] = "not-a-hash"
            variants.append(bad_hash)
            mismatched_hash = json.loads(json.dumps(baseline))
            mismatched_hash["input"]["text"] = "Different text"
            variants.append(mismatched_hash)
            missing_voice_asset = json.loads(json.dumps(baseline))
            del missing_voice_asset["voice"]["asset_sha256"]
            missing_voice_asset["voice_sha256"] = bound(
                missing_voice_asset["voice"])[1]
            variants.append(missing_voice_asset)
            same_seed = json.loads(json.dumps(baseline))
            same_seed["attempts"][1]["seed"] = 1
            variants.append(same_seed)
            wrong_number = json.loads(json.dumps(baseline))
            wrong_number["attempts"][1]["attempt"] = 3
            variants.append(wrong_number)
            same_take = json.loads(json.dumps(baseline))
            same_take["attempts"][1]["take_id"] = same_take["attempts"][0]["take_id"]
            variants.append(same_take)
            bad_take = json.loads(json.dumps(baseline))
            bad_take["attempts"][1]["take_id"] = "bad\ud800take"
            variants.append(bad_take)
            same_recording = json.loads(json.dumps(baseline))
            same_recording["attempts"][1]["audio"] = "audio-1.wav"
            variants.append(same_recording)
            escaping = json.loads(json.dumps(baseline))
            escaping["attempts"][0]["audio"] = "../audio.wav"
            variants.append(escaping)
            one_attempt = json.loads(json.dumps(baseline))
            one_attempt["attempts"].pop()
            variants.append(one_attempt)
            for value in variants:
                if value is escaping:
                    write_wav(root.parent / "audio.wav", 0)
                manifest.write_text(json.dumps({"version": 1, "cases": [value]}),
                                    encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(retry.EndpointScreenError):
                    retry.build_retry_report(manifest)
            second = case("two", "audio-3.wav", "audio-4.wav", seeds=(3, 4))
            duplicate_id = json.loads(json.dumps(second))
            duplicate_id["id"] = "one"
            duplicate_take = json.loads(json.dumps(second))
            duplicate_take["attempts"][0]["take_id"] = baseline["attempts"][0]["take_id"]
            reused_audio = json.loads(json.dumps(second))
            reused_audio["attempts"][0]["audio"] = "audio-1.wav"
            for value in (duplicate_id, duplicate_take, reused_audio):
                manifest.write_text(json.dumps({"version": 1,
                                                "cases": [baseline, value]}),
                                    encoding="utf-8")
                with self.subTest(cross_case=value), self.assertRaises(
                        retry.EndpointScreenError):
                    retry.build_retry_report(manifest)
            manifest.write_text(json.dumps({"version": 1, "cases": [baseline]}),
                                encoding="utf-8")
            for threshold in (True, "-40", None, float("nan"), 1.0, -121.0,
                              10**10_000):
                with self.subTest(threshold=threshold), self.assertRaisesRegex(
                        retry.EndpointScreenError, "threshold"):
                    retry.build_retry_report(manifest, threshold)
            original_limit = retry.MAX_TOTAL_AUDIO_BYTES
            try:
                retry.MAX_TOTAL_AUDIO_BYTES = (root / "audio-1.wav").stat().st_size
                with self.assertRaisesRegex(retry.EndpointScreenError, "audio work"):
                    retry.build_retry_report(manifest)
            finally:
                retry.MAX_TOTAL_AUDIO_BYTES = original_limit

    def test_cli_writes_provenance_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "cut.wav", 12000)
            write_wav(root / "clean.wav", 0)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "cases": [
                case("retry", "cut.wav", "clean.wav")]}), encoding="utf-8")
            output = root / "report.json"
            command = [sys.executable, "-m", "experiments.tts_endpoint_retry_screen",
                       "--manifest", str(manifest), "--out", str(output)]
            first = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(0, first.returncode, first.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("tts_endpoint_retry_screen.py",
                             written["provenance"]["script"])
            self.assertEqual(manifest.name,
                             written["provenance"]["args"]["manifest"])
            self.assertEqual(output.name, written["provenance"]["args"]["out"])
            self.assertEqual(-40.0,
                             written["provenance"]["args"]["threshold_dbfs"])
            before = output.read_bytes()
            second = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(3, second.returncode)
            self.assertNotIn("Traceback", second.stderr)
            self.assertEqual(before, output.read_bytes())
            invalid_output = root / "invalid.json"
            invalid = subprocess.run(
                [*command[:-1], str(invalid_output), "--threshold-dbfs", "nan"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                text=True, timeout=15)
            self.assertEqual(2, invalid.returncode)
            self.assertIn("refused input", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)
            self.assertFalse(invalid_output.exists())
