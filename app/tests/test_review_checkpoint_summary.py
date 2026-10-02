"""Review checkpoint polling must tolerate a file removed before its stat."""

from tests.test_support import assert_file_lock_released
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor


CHECKPOINT = {"completed_batches": 2, "total_batches": 4,
              "failed_batches": [2], "batch_lengths": [3, 4],
              "all_corrected": [{"text": "done"}], "batch_size": 10,
              "context_window": 5, "total_stats": {"batches_skipped_vram": 1,
              "text_changed": 3, "speaker_changed": 2}}


class ReviewCheckpointSummaryTests(unittest.TestCase):
    def test_actual_listing_skips_malformed_summary_fields_without_changing_files(self):
        cases = [("completed_batches", "two"), ("total_batches", []),
                 ("failed_batches", 1), ("failed_batches", [1, "two"]),
                 ("batch_lengths", 1), ("total_stats", [1]),
                 ("all_corrected", 1)]
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "annotated_script.json")
                active = Path(str(script) + ".review_checkpoint.json")
                scripts = Path(tmp, "scripts")
                scripts.mkdir()
                valid = scripts / "valid.json.review_checkpoint.json"
                malformed = {**CHECKPOINT, field: value}
                active.write_text(json.dumps(malformed))
                valid.write_text(json.dumps(CHECKPOINT))
                before = {p: p.read_bytes() for p in (active, valid)}
                app = FastAPI()
                app.include_router(editor.router)
                with patch.object(editor, "SCRIPT_PATH", str(script)), \
                     patch.object(editor, "SCRIPTS_DIR", str(scripts)), \
                     TestClient(app, raise_server_exceptions=False) as client:
                    response = client.get("/api/review/checkpoints")
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(["valid"], [row["book"] for row in response.json()["checkpoints"]])
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_missing_and_null_optional_fields_keep_existing_empty_summary(self):
        for fields in ({"completed_batches": 0}, {"completed_batches": None,
                "total_batches": None, "failed_batches": None, "batch_lengths": None,
                "total_stats": None, "all_corrected": None}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "checkpoint.json")
                path.write_text(json.dumps(fields))
                before = path.read_bytes()
                summary = editor._summarize_review_checkpoint(str(path))
                self.assertEqual(0, summary["completed_batches"])
                self.assertEqual(0, summary["total_batches"])
                self.assertEqual(0, summary["entries_done"])
                self.assertEqual([], summary["failed_batches"])
                self.assertEqual(1, summary["resume_from_batch"])
                self.assertEqual(0, summary["batches_skipped_vram"])
                self.assertEqual(before, path.read_bytes())

    def test_actual_http_listing_skips_file_removed_at_stat_and_keeps_other_checkpoint(self):
        for active in (True, False):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / "annotated_script.json"
                scripts = root / "scripts"
                scripts.mkdir()
                removed = (Path(str(script) + ".review_checkpoint.json") if active else
                           scripts / "removed.json.review_checkpoint.json")
                kept = scripts / "kept.json.review_checkpoint.json"
                for path in (removed, kept):
                    path.write_text(json.dumps(CHECKPOINT))
                actual_getmtime = os.path.getmtime
                stats = []

                def delete_at_stat(path):
                    stats.append(str(path))
                    if Path(path) == removed:
                        # The old exists() check already returned True here.
                        removed.unlink()
                    return actual_getmtime(path)

                app = FastAPI()
                app.include_router(editor.router)
                with patch.object(editor, "SCRIPT_PATH", str(script)), \
                     patch.object(editor, "SCRIPTS_DIR", str(scripts)), \
                     patch.object(editor, "process_state", {}), \
                     patch.object(editor.os.path, "getmtime", side_effect=delete_at_stat), \
                     TestClient(app) as client:
                    response = client.get("/api/review/checkpoints")
                self.assertEqual(200, response.status_code, response.text)
                result = response.json()
                self.assertEqual(["kept"], [item["book"] for item in result["checkpoints"]])
                self.assertEqual(2, result["checkpoints"][0]["resume_from_batch"])
                self.assertIsNone(result["live"])
                self.assertEqual(1, stats.count(str(removed)))
                self.assertFalse(removed.exists())
                self.assertEqual(CHECKPOINT, json.loads(kept.read_text()))

    def test_actual_listing_preserves_summary_fields_sort_order_and_live_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "annotated_script.json")
            active = Path(str(script) + ".review_checkpoint.json")
            scripts = Path(tmp, "scripts")
            scripts.mkdir()
            saved = scripts / "saved.json.review_checkpoint.json"
            for path, mtime in ((active, 100), (saved, 200)):
                path.write_text(json.dumps(CHECKPOINT))
                os.utime(path, (mtime, mtime))
            app = FastAPI()
            app.include_router(editor.router)
            state = {"batch_review": {"running": True, "bidirectional": True,
                     "current_pass": "bwd", "current_task_idx": 0,
                     "tasks": [{"name": "saved", "status": "running"}, None]}}
            with patch.object(editor, "SCRIPT_PATH", str(script)), \
                 patch.object(editor, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(editor, "process_state", state), TestClient(app) as client:
                response = client.get("/api/review/checkpoints")
            self.assertEqual(200, response.status_code)
            result = response.json()
            self.assertEqual(["saved", "(active script)"], [item["book"] for item in result["checkpoints"]])
            summary = result["checkpoints"][0]
            self.assertEqual({"book": "saved", "completed_batches": 2,
                "total_batches": 4, "resume_from_batch": 2, "entries_done": 1,
                "batch_size": 10, "context_window": 5, "failed_batches": [2],
                "batches_skipped_vram": 1, "text_changed": 3, "speaker_changed": 2,
                "mtime": 200.0}, summary)
            self.assertEqual({"bidirectional": True, "current_pass": "bwd",
                "current_task_idx": 0, "tasks": [{"name": "saved", "status": "running"}]}, result["live"])

    def test_unusable_checkpoint_shapes_still_return_no_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "checkpoint.json")
            for data in ([], None, {"total_batches": 1}):
                path.write_text(json.dumps(data))
                self.assertIsNone(editor._summarize_review_checkpoint(str(path)))

    def test_unexpected_stat_error_is_not_silently_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "checkpoint.json")
            path.write_text(json.dumps(CHECKPOINT))
            with patch.object(editor.os.path, "getmtime", side_effect=PermissionError("denied")), \
                 self.assertRaisesRegex(PermissionError, "denied"):
                editor._summarize_review_checkpoint(str(path))


class ReviewCheckpointWriteLockTests(unittest.TestCase):
    @staticmethod
    def _save(output):
        import review_script
        review_script.save_checkpoint(output, 1, 2, 10, 5,
                                      [{'text':'new progress'}], {'entries_changed':1},
                                      None, [1], [], 'source-hash', 'output-hash')

    def test_delete_waits_for_inflight_save_and_does_not_recreate_checkpoint(self):
        from contextlib import contextmanager
        import threading
        import review_script
        from utils import atomic_json_write, file_lock
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = json.dumps({'prior':'checkpoint'})
            path.write_text(prior)
            writer_paused = threading.Event()
            release = threading.Event()
            delete_attempted = threading.Event()
            errors = []

            def publish(data, target):
                writer_paused.set()
                if not release.wait(3):
                    raise RuntimeError('Test did not release the checkpoint publisher')
                return atomic_json_write(data, target)

            @contextmanager
            def observed_lock(target, **kwargs):
                if threading.current_thread().name == 'checkpoint-deleter':
                    delete_attempted.set()
                with file_lock(target, **kwargs):
                    yield

            def run(callback):
                try:
                    callback()
                except BaseException as exc:
                    errors.append(exc)

            writer = threading.Thread(target=run, args=(lambda:self._save(output),),
                                      name='checkpoint-writer')
            deleter = threading.Thread(target=run, args=(lambda:review_script.clear_checkpoint(output),),
                                       name='checkpoint-deleter')
            with patch.object(review_script, 'atomic_json_write', side_effect=publish), \
                 patch.object(review_script, 'file_lock', observed_lock):
                writer.start()
                try:
                    self.assertTrue(writer_paused.wait(2))
                    deleter.start()
                    self.assertTrue(delete_attempted.wait(2))
                    deleter.join(0.1)
                    waited = deleter.is_alive()
                    preserved = path.exists() and path.read_text() == prior
                finally:
                    release.set()
                    writer.join(3)
                    if deleter.ident is not None:
                        deleter.join(3)
            self.assertFalse(writer.is_alive())
            self.assertFalse(deleter.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(waited, 'Deletion bypassed the in-flight publisher')
            self.assertTrue(preserved, 'Deletion removed the checkpoint before publication completed')
            self.assertFalse(path.exists(), 'The writer recreated a checkpoint after deletion')
            assert_file_lock_released(str(path))

    def test_publication_failure_preserves_old_checkpoint_and_releases_lock(self):
        import review_script
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = b'{"prior":"checkpoint"}'
            path.write_bytes(prior)
            with patch.object(review_script, 'atomic_json_write', side_effect=PermissionError('fixture denied')), \
                 patch('builtins.print') as diagnostic:
                self._save(output)
            self.assertEqual(prior, path.read_bytes())
            assert_file_lock_released(str(path))
            self.assertIn('Failed to save checkpoint', str(diagnostic.call_args))
            self.assertIn('fixture denied', str(diagnostic.call_args))

    def test_lock_timeout_keeps_checkpoint_and_reports_existing_warning_contract(self):
        import review_script
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'book.json'))
            path = Path(review_script._checkpoint_path(output))
            prior = b'{"prior":"checkpoint"}'
            path.write_bytes(prior)
            with patch.object(review_script, 'file_lock', side_effect=TimeoutError('fixture held')), \
                 patch('builtins.print') as diagnostic:
                self._save(output)
            self.assertEqual(prior, path.read_bytes())
            self.assertIn('Failed to save checkpoint', str(diagnostic.call_args))
            self.assertIn('fixture held', str(diagnostic.call_args))
