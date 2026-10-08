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
    def test_status_words_in_names_do_not_clear_start_but_terminal_fields_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("LOCK_FAILED_retry", "PENDING_FAILED_retry", "KILLED_retry", "ordinary"):
                with self.subTest(name=name):
                    start = f"2026-10-01T00:00:00Z START    {name}"
                    (root / "q.log").write_text(start + "\nstamp QUEUED   LOCK_FAILED_retry\n")
                    result = self.run_shell(root, "get_logged_queue_job")
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(name, result.stdout.strip())
                    for status in ("OK", "FAILED", "REFUSED", "NO_VRAM", "NO_LLM", "KILLED",
                                   "LOCK_FAILED", "PENDING_FAILED", "INTERRUPTED", "STOPPED"):
                        (root / "q.log").write_text(start + f"\nstamp {status:<9} {name}\n")
                        result = self.run_shell(root, "get_logged_queue_job")
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertEqual("", result.stdout)

    def test_unrelated_terminal_preserves_active_owner_and_real_completion_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("active", "active job", "active (queued)"):
                start = f"stamp START    {name} owner_pid=123"
                for status in ("FAILED", "LOCK_FAILED", "PENDING_FAILED"):
                    (root / "q.log").write_text(start + f"\nstamp {status} {name} owner_pid=456\n")
                    result = self.run_shell(root, "get_logged_queue_job")
                    self.assertEqual(name, result.stdout.strip(), result.stderr)
                (root / "q.log").write_text(start + f"\nstamp FAILED {name} rc=2 owner_pid=123\n")
                self.assertEqual("", self.run_shell(root, "get_logged_queue_job").stdout)
            (root / "q.log").write_text("stamp START    active job\nstamp PENDING_FAILED other job\n")
            self.assertEqual("active job", self.run_shell(root, "get_logged_queue_job").stdout.strip())
            with open(root / "q.log", "a") as stream:
                stream.write("stamp FAILED active job rc=2\n")
            self.assertEqual("", self.run_shell(root, "get_logged_queue_job").stdout)

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
