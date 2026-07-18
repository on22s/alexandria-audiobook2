import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preparer_benchmark import execute_fixture


class PreparerBenchmarkTests(unittest.TestCase):
    def test_changed_audio_is_rejected_before_preparer_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "audio.wav").write_bytes(b"changed")
            fixture = {"root_dir": tmp, "audio_path": "audio.wav",
                       "audio_sha256": hashlib.sha256(b"original").hexdigest(),
                       "limit": 1, "language": "en", "model_revision": "rev"}
            with patch("preparer_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "audio hash changed"):
                execute_fixture(fixture, "python", "preparer.py")
        run.assert_not_called()

    def test_worker_reports_transcript_identity_from_preparer_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "audio.wav").write_bytes(b"audio")
            fixture = {"root_dir": tmp, "audio_path": "audio.wav",
                       "audio_sha256": hashlib.sha256(b"audio").hexdigest(),
                       "limit": 1, "language": "en", "model_revision": "rev"}

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--asr-output") + 1])
                output_path.write_text(json.dumps({
                    "detected_lang": "en", "audio_duration": 30.0,
                    "word_segments": [{"word": "HELLO", "start": 0.0, "end": 0.5}]}))
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("preparer_benchmark.subprocess.run", side_effect=fake_run):
                metrics = execute_fixture(fixture, "python", "preparer.py")
        self.assertEqual(1, metrics["word_count"])
        self.assertEqual(64, len(metrics["transcript_text_sha256"]))
        self.assertEqual(64, len(metrics["alignment_sha256"]))


if __name__ == "__main__":
    unittest.main()
