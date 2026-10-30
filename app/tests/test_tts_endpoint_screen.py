import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

from experiments import tts_endpoint_screen as screen


def write_wav(path, tail, prefix=None, rate=24000):
    samples = (prefix if prefix is not None else [0] * 720) + list(tail)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))


class TTSEndpointScreenTests(unittest.TestCase):
    def test_labeled_screen_reports_confusion_matrix_and_all_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "cut.wav", [8000] * 720)
            write_wav(root / "clean.wav", [0] * 720)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "rows": [
                {"id": "cut", "label": "cut", "audio": "cut.wav"},
                {"id": "clean", "label": "clean", "audio": "clean.wav"}]}),
                encoding="utf-8")
            report = screen.build_report(manifest)
            self.assertEqual({"true_positive": 1, "false_positive": 0,
                              "true_negative": 1, "false_negative": 0},
                             report["confusion_matrix"])
            self.assertEqual({"5", "10", "20", "30"},
                             set(report["rows"][0]["tail_rms_dbfs"]))
            self.assertEqual("measured_not_production_qualified", report["status"])

    def test_labels_are_not_inferred_from_the_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "loud-clean.wav", [32767] * 120)
            write_wav(root / "quiet-cut.wav", [0] * 120)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "rows": [
                {"id": "clean", "label": "clean", "audio": "loud-clean.wav"},
                {"id": "cut", "label": "cut", "audio": "quiet-cut.wav"}]}),
                encoding="utf-8")
            report = screen.build_report(manifest)
            self.assertEqual(1, report["confusion_matrix"]["false_positive"])
            self.assertEqual(1, report["confusion_matrix"]["false_negative"])

    def test_malformed_and_escaping_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            bad = [
                {"version": 1, "rows": [{"id": "x", "label": "maybe", "audio": "x.wav"}]},
                {"version": 1, "rows": [{"id": "x", "label": [], "audio": "x.wav"}]},
                {"version": 1, "rows": [{"id": "x", "label": "clean", "audio": "a\u0000b.wav"}]},
                {"version": 1, "rows": [{"id": "x", "label": "clean", "audio": "a\ud800b.wav"}]},
                {"version": 1, "rows": [{"id": "x", "label": "clean", "audio": "../x.wav"}]},
                {"version": 1, "rows": []},
                {"version": True, "rows": [{"id": "x", "label": "clean", "audio": "x.wav"}]},
                {"version": 1.0, "rows": [{"id": "x", "label": "clean", "audio": "x.wav"}]},
            ]
            for value in bad:
                manifest.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(screen.EndpointScreenError):
                    screen.build_report(manifest)

    def test_truncated_pcm_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "short.wav"
            write_wav(audio, [0] * 120)
            raw = bytearray(audio.read_bytes())
            raw[40:44] = struct.pack("<I", 2000)
            audio.write_bytes(raw[:-20])
            with self.assertRaisesRegex(screen.EndpointScreenError, "RIFF length"):
                screen.measure_wav_endpoint(audio)
            audio.write_bytes(raw[:-21])
            with self.assertRaisesRegex(screen.EndpointScreenError, "RIFF length"):
                screen.measure_wav_endpoint(audio)

    def test_odd_declared_data_and_too_short_audio_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "odd.wav"
            write_wav(audio, [0] * 120)
            raw = bytearray(audio.read_bytes())
            raw[40:44] = struct.pack("<I", 1201)
            audio.write_bytes(raw)
            with self.assertRaisesRegex(screen.EndpointScreenError, "aligned"):
                screen.measure_wav_endpoint(audio)
            short = root / "short.wav"
            write_wav(short, [0] * 1, prefix=[])
            with self.assertRaisesRegex(screen.EndpointScreenError, "shorter"):
                screen.measure_wav_endpoint(short)

    def test_relative_path_and_bad_riff_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "audio.wav"
            write_wav(audio, [0] * 120)
            self.assertEqual(24000, screen.measure_wav_endpoint(audio)["sample_rate"])
            raw = bytearray(audio.read_bytes())
            raw[4:8] = struct.pack("<I", 0)
            audio.write_bytes(raw)
            with self.assertRaisesRegex(screen.EndpointScreenError, "RIFF length"):
                screen.measure_wav_endpoint(audio)

    def test_manifest_and_audio_limits_are_checked_before_full_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_bytes(b" " * 20)
            original_manifest_limit = screen.MAX_MANIFEST_BYTES
            try:
                screen.MAX_MANIFEST_BYTES = 10
                with self.assertRaisesRegex(screen.EndpointScreenError, "too large"):
                    screen.build_report(manifest)
            finally:
                screen.MAX_MANIFEST_BYTES = original_manifest_limit
            audio = root / "audio.wav"
            write_wav(audio, [0] * 120)
            original_audio_limit = screen.MAX_AUDIO_BYTES
            try:
                screen.MAX_AUDIO_BYTES = 10
                with self.assertRaisesRegex(screen.EndpointScreenError, "exceeds"):
                    screen._resolve_audio(root, "audio.wav")
            finally:
                screen.MAX_AUDIO_BYTES = original_audio_limit

    def test_aggregate_audio_work_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "audio.wav", [0] * 120)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "rows": [
                {"id": "one", "label": "clean", "audio": "audio.wav"},
                {"id": "two", "label": "clean", "audio": "audio.wav"}]}),
                encoding="utf-8")
            original = screen.MAX_TOTAL_AUDIO_BYTES
            try:
                screen.MAX_TOTAL_AUDIO_BYTES = (root / "audio.wav").stat().st_size
                with self.assertRaisesRegex(screen.EndpointScreenError, "audio work"):
                    screen.build_report(manifest)
            finally:
                screen.MAX_TOTAL_AUDIO_BYTES = original

    def test_cli_writes_once_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wav(root / "clean.wav", [0] * 120)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "rows": [
                {"id": "clean", "label": "clean", "audio": "clean.wav"}]}),
                encoding="utf-8")
            output = root / "report.json"
            command = [sys.executable, "-m", "experiments.tts_endpoint_screen",
                       "--manifest", str(manifest), "--out", str(output)]
            first = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(0, first.returncode, first.stderr)
            before = output.read_bytes()
            second = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(0, second.returncode)
            self.assertNotIn("Traceback", second.stderr)
            self.assertIn("could not publish report", second.stderr)
            self.assertEqual(before, output.read_bytes())
