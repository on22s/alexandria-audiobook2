import builtins
import hashlib
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preparer_benchmark import execute_fixture


class PreparerBenchmarkTests(unittest.TestCase):
    def test_large_audio_hash_uses_bounded_reads_and_preserves_exact_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp, "audio.wav")
            raw = b"0123456789abcdef" * (256 * 1024) + b"last partial block"
            audio.write_bytes(raw)
            original_hash = hashlib.sha256(raw).hexdigest()
            fixture = {"root_dir": tmp, "audio_path": "audio.wav",
                       "audio_sha256": original_hash, "limit": 1,
                       "language": "en", "model_revision": "fixture-rev"}
            # An actual CPU child exercises command/output parsing; no ASR model runs.
            worker = Path(tmp, "cpu_worker.py")
            worker.write_text(
                "import argparse,json\n"
                "from pathlib import Path\n"
                "p=argparse.ArgumentParser()\n"
                "p.add_argument('--asr-output');p.add_argument('--audio')\n"
                "a,_=p.parse_known_args()\n"
                "assert Path(a.audio).is_file()\n"
                "Path(a.asr_output).write_text(json.dumps({'detected_lang':'en',"
                "'audio_duration':1.25,'word_segments':[{'word':'HELLO','start':0,'end':0.5}]}))\n")
            reads = []

            class BoundedAudioReader:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

                def read(self, size=-1):
                    self_test.assertGreater(size, 0, "hashing cannot request an unbounded read")
                    self_test.assertLessEqual(size, 1024 * 1024)
                    block = self.handle.read(size)
                    reads.append(len(block))
                    return block

            self_test = self

            def tracked_open(path, mode="r", *args, **kwargs):
                handle = builtins.open(path, mode, *args, **kwargs)
                if Path(path) == audio and mode == "rb":
                    return BoundedAudioReader(handle)
                return handle

            with patch("lora_evidence.open", side_effect=tracked_open):
                metrics = execute_fixture(fixture, sys.executable, str(worker))
            self.assertEqual([1024 * 1024] * 4 + [len(b"last partial block"), 0], reads)
            self.assertEqual(len(raw), sum(reads))
            self.assertEqual(original_hash, hashlib.sha256(audio.read_bytes()).hexdigest())
            self.assertEqual(1, metrics["word_count"])
            self.assertEqual("en", metrics["detected_language"])
            self.assertEqual(1.25, metrics["audio_duration_seconds"])
            self.assertEqual(hashlib.sha256(b"HELLO").hexdigest(), metrics["transcript_text_sha256"])
            self.assertEqual(hashlib.sha256(json.dumps(
                [{"word": "HELLO", "start": 0, "end": 0.5}], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(), metrics["alignment_sha256"])

    def test_audio_path_cannot_escape_fixture_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp).parent / "outside.wav"
            outside.write_bytes(b"audio")
            fixture = {"root_dir": tmp, "audio_path": "../outside.wav",
                       "audio_sha256": hashlib.sha256(b"audio").hexdigest(),
                       "limit": 1, "language": "en", "model_revision": "rev"}
            with self.assertRaisesRegex(ValueError, "inside fixture root"):
                execute_fixture(fixture, "python", "preparer.py")
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
