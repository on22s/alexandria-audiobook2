"""Native concurrent queue writers, retention, and active status after rotation."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
HELPER = Path(os.environ.get("QUEUE_ROTATION_HELPER", REPO / "run_chains/lib/gpu_queue_log.sh"))
LIMIT = 8388608


class QueueRotationTests(unittest.TestCase):
    def run_shell(self, root, body, *args):
        return subprocess.run(["bash", "-c", 'source "$1"; QLOG="$2"; ' + body,
                               "fixture", str(HELPER), str(root / "q.log"), *args],
                              capture_output=True, text=True, timeout=30)

    def test_retention_preserves_active_start_beyond_all_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qlog = root / "q.log"
            start = "2026-10-01T00:00:00Z START    日本語 job"
            for index in range(6):
                previous = qlog.read_text() if qlog.exists() else start + "\n"
                qlog.write_text(previous + "x" * (LIMIT - len(previous.encode()) - 1) + "\n")
                result = self.run_shell(root, 'append_queue_log "stamp QUEUED   event$3"; get_logged_queue_job', str(index))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("日本語 job", result.stdout.strip())
                self.assertLessEqual(qlog.stat().st_size, LIMIT)
                self.assertTrue((root / "q.log.1").exists())
            self.assertEqual(3, len(list(root.glob("q.log.[0-9]"))))
            result = self.run_shell(root, 'append_queue_log "stamp OK       日本語 job"; get_logged_queue_job')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stdout)

    def test_concurrent_writers_rotate_without_losing_or_splitting_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # > one log, < total retention; every record must survive exactly once.
            processes = []
            body = 'source "$1"; QLOG="$2"; printf -v padding "%65500s" ""; for ((i=0;i<40;i++)); do append_queue_log "$3:$i:$padding" || exit; done'
            for index in range(6):
                processes.append(subprocess.Popen(["bash", "-c", body, "fixture", str(HELPER), str(root / "q.log"), str(index)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            try:
                for process in processes:
                    out, err = process.communicate(timeout=30)
                    self.assertEqual(0, process.returncode, err)
                records = []
                for p in [root / "q.log.3", root / "q.log.2", root / "q.log.1", root / "q.log"]:
                    if p.exists():
                        self.assertLessEqual(p.stat().st_size, LIMIT)
                        records.extend(p.read_text().splitlines())
                self.assertEqual(240, len(records))
                self.assertEqual({f"{j}:{i}" for j in range(6) for i in range(40)}, {r.rsplit(":", 1)[0] for r in records})
                self.assertTrue(all(len(r.rsplit(":", 1)[1]) == 65500 for r in records))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)

    def test_pause_and_resume_use_rotation_and_fail_loud_on_bad_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qlog = root / "q.log"
            qlog.write_bytes(b"x" * (LIMIT - 1) + b"\n")
            env = {**os.environ, "GPU_QLOG": str(qlog), "GPU_PAUSE_FLAG": str(root / "paused")}
            for action in ("on", "off"):
                result = subprocess.run(["bash", str(REPO / "gpu_pause.sh"), action], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "q.log.1").exists())
            self.assertIn("PAUSED", qlog.read_text())
            self.assertIn("RESUMED", qlog.read_text())
            qlog.unlink()
            qlog.mkdir()
            result = subprocess.run(["bash", str(REPO / "gpu_pause.sh"), "off"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(8, result.returncode, result.stderr)
            self.assertNotIn("queue released", result.stdout)

    def test_actual_status_retains_live_job_across_rotation(self):
        from tests import test_gpu_pause_status_batch as status_tests
        with status_tests.PauseStatusBatchTests.fixture(self) as (root, env, child):
            qlog = root / "q.log"
            start = qlog.read_bytes()
            qlog.write_bytes(start + b"x" * (LIMIT - len(start) - 1) + b"\n")
            result = self.run_shell(root, 'append_queue_log "stamp QUEUED   waiting"')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("running job: long job\n", status_tests.PauseStatusBatchTests.status(self, env))
            result = self.run_shell(root, 'append_queue_log "stamp OK       long job"')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("running job: none\n", status_tests.PauseStatusBatchTests.status(self, env))

    def test_bad_archive_destination_refuses_record_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qlog = root / "q.log"
            original = b"x" * (LIMIT - 1) + b"\n"
            qlog.write_bytes(original)
            archive = root / "q.log.1"
            archive.mkdir()
            result = self.run_shell(root, 'append_queue_log "stamp QUEUED   refused"')
            self.assertEqual(8, result.returncode, result.stderr)
            self.assertIn("queue log update failed", result.stderr)
            self.assertEqual(original, qlog.read_bytes())
            self.assertEqual([], list(archive.iterdir()))
            archive.rmdir()
            result = self.run_shell(root, 'append_queue_log "stamp QUEUED   next"')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("next", qlog.read_text())
