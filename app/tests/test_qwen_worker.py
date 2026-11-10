"""Qwen worker process plumbing (#1844) — owned subprocess, bounded IPC.

Runs the real run_qwen_alignment parent helper against the test-only
fake worker fixture (app/tests/fixtures/qwen_fake_worker.py). No real
model, torch or qwen_asr is loaded in this module.
"""
import json
import os
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path

import qwen_runtime as qr


def _write_wav(path, frames=16000, rate=16000):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * (frames // 2 if frames % 2 else frames))


class RunAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.worker = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"
        self.assertTrue(self.worker.is_file(), "fake worker fixture must exist")
        self.job_path = self.dir / "job.json"
        self.result_path = self.dir / "result.json"
        self.wav = self.dir / "row0.wav"
        _write_wav(self.wav)
        self.job_path.write_text(json.dumps({
            "rows": [{"id": "row0", "wav_path": str(self.wav),
                      "text": "Hello synthetic world.", "duration_sec": 1.0}],
            "language": "English", "device": "cpu"}), encoding="utf-8")

    def test_fake_worker_roundtrip(self):
        tokens = qr.run_qwen_alignment(
            [sys_executable(), str(self.worker)], self.job_path, self.result_path,
            cancel_check=lambda: False)
        rows = tokens["row0"]
        self.assertEqual(["Hello", "synthetic", "world"], [t["text"] for t in rows])
        self.assertLess(rows[0]["start_time"], rows[-1]["end_time"])

    def test_cancel_before_start_launches_nothing(self):
        with self.assertRaises(ValueError):
            qr.run_qwen_alignment(
                [sys_executable(), str(self.worker)], self.job_path, self.result_path,
                cancel_check=lambda: True)
        self.assertFalse(self.result_path.exists())

    def test_malformed_result_fails_closed(self):
        bad = self.dir / "bad_worker.py"
        bad.write_text("import json, sys\nopen(sys.argv[2],'w').write('{not json')\n",
                       encoding="utf-8")
        with self.assertRaises(ValueError):
            qr.run_qwen_alignment(
                [sys_executable(), str(bad)], self.job_path, self.result_path,
                cancel_check=lambda: False)

    def test_oversized_result_fails_closed(self):
        big = self.dir / "big_worker.py"
        big.write_text("import json, sys\n"
                       "open(sys.argv[2],'wb').write(b'x' * %d)\n"
                       % (qr.MAX_RESULT_BYTES + 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            qr.run_qwen_alignment(
                [sys_executable(), str(big)], self.job_path, self.result_path,
                cancel_check=lambda: False, max_result_bytes=1024)

    def test_worker_error_preserved(self):
        failing = self.dir / "fail_worker.py"
        failing.write_text("import json, sys\n"
                           "json.dump({'ok': False, 'error': 'boom: test failure'}, open(sys.argv[2],'w'))\n"
                           "sys.exit(1)\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            qr.run_qwen_alignment(
                [sys_executable(), str(failing)], self.job_path, self.result_path,
                cancel_check=lambda: False)
        self.assertIn("boom: test failure", str(caught.exception))

    def test_timeout_kills_hanging_worker(self):
        slow = self.dir / "slow_worker.py"
        slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            qr.run_qwen_alignment(
                [sys_executable(), str(slow)], self.job_path, self.result_path,
                cancel_check=lambda: False, timeout=2)
        self.assertIn("timed out", str(caught.exception))

    def test_real_cancel_kills_child(self):
        """Cancel mid-run terminates the child process (owned, reaped)."""
        slow = self.dir / "slow_worker.py"
        slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        cancelled = threading.Event()

        def cancel_check():
            # First polls return False, then True once cancel is requested.
            return cancelled.is_set()

        done = threading.Event()
        result = []

        def run():
            try:
                qr.run_qwen_alignment(
                    [sys_executable(), str(slow)], self.job_path, self.result_path,
                    cancel_check=cancel_check, timeout=30)
            except ValueError as error:
                result.append(str(error))
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # Give the child time to start, then cancel.
        import time
        time.sleep(0.5)
        cancelled.set()
        self.assertTrue(done.wait(timeout=10), "cancel did not terminate the child")
        self.assertTrue(any("cancelled" in message.lower() for message in result))


def sys_executable():
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main()
