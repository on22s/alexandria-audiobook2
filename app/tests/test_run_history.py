import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import core
from run_history import (finish_run, get_run, list_runs, mark_interrupted_runs,
                         prune_runs, record_artifact, start_run, update_run)


class RunHistoryTests(unittest.TestCase):
    def test_run_lifecycle_is_persisted(self):
        with tempfile.TemporaryDirectory() as history_dir:
            run_id = start_run(history_dir, "batch_review")
            running = get_run(history_dir, run_id)
            finished = finish_run(history_dir, run_id, "completed")

            self.assertEqual("running", running["status"])
            self.assertEqual("completed", finished["status"])
            self.assertIsNotNone(finished["finished_at"])
            self.assertEqual([run_id], [item["id"] for item in list_runs(history_dir)])

    def test_invalid_run_id_cannot_escape_history_directory(self):
        with tempfile.TemporaryDirectory() as history_dir:
            self.assertIsNone(get_run(history_dir, "../state"))

    def test_artifact_records_output_source_and_config_hashes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            history_dir = os.path.join(data_dir, "run_history")
            output = os.path.join(data_dir, "report.md")
            source = os.path.join(data_dir, "book.json")
            config = os.path.join(data_dir, "config.json")
            for path, content in ((output, b"report"), (source, b"source"),
                                  (config, b"config")):
                with open(path, "wb") as handle:
                    handle.write(content)
            run_id = start_run(history_dir, "batch_review")

            artifact = record_artifact(
                history_dir, run_id, output, "batch_review_report", data_dir,
                source_paths=[source], config_path=config)

            self.assertEqual("report.md", artifact["path"])
            self.assertEqual(64, len(artifact["sha256"]))
            self.assertEqual("book.json", artifact["sources"][0]["path"])
            self.assertEqual("config.json", artifact["config"]["path"])

    def test_shared_runner_records_failure_and_releases_task(self):
        key = "_test_history_failure"
        core.process_state[key] = {"running": True, "logs": [], "process": None}
        try:
            with tempfile.TemporaryDirectory() as history_dir, \
                 patch.object(core, "RUN_HISTORY_DIR", history_dir):
                core._run_claimed_background_task(
                    key, lambda: (_ for _ in ()).throw(OSError("launch failed")))
                records = list_runs(history_dir)

            self.assertEqual("failed", records[0]["status"])
            self.assertEqual("launch failed", records[0]["error"])
            self.assertFalse(core.process_state[key]["running"])
        finally:
            core.process_state.pop(key, None)

    def test_history_failure_does_not_block_task(self):
        key = "_test_history_unavailable"
        callback = Mock()
        core.process_state[key] = {"running": True, "logs": [], "process": None}
        try:
            with patch.object(core, "start_run", side_effect=OSError("disk unavailable")):
                core._run_claimed_background_task(key, callback)
            callback.assert_called_once_with()
            self.assertFalse(core.process_state[key]["running"])
        finally:
            core.process_state.pop(key, None)

    def test_run_summary_updates_atomically_without_changing_identity(self):
        with tempfile.TemporaryDirectory() as history_dir:
            run_id = start_run(history_dir, "voicelab")
            original = get_run(history_dir, run_id)
            updated = update_run(history_dir, run_id, {
                "stages": [{"name": "train", "status": "completed"}],
                "next_action": "Review adapters.",
            })
        self.assertEqual(run_id, updated["id"])
        self.assertEqual(original["started_at"], updated["started_at"])
        self.assertEqual("completed", updated["stages"][0]["status"])

    def test_failed_summary_write_preserves_previous_record(self):
        with tempfile.TemporaryDirectory() as history_dir:
            run_id = start_run(history_dir, "voicelab")
            before = get_run(history_dir, run_id)
            with patch("run_history.atomic_json_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    update_run(history_dir, run_id, {"stages": [{"status": "failed"}]})
            self.assertEqual(before, get_run(history_dir, run_id))

    def test_startup_marks_only_running_records_interrupted(self):
        with tempfile.TemporaryDirectory() as history_dir:
            running_id = start_run(history_dir, "voicelab")
            completed_id = start_run(history_dir, "review")
            finish_run(history_dir, completed_id, "completed")
            changed = mark_interrupted_runs(history_dir)
            self.assertEqual([running_id], [item["id"] for item in changed])
            self.assertEqual("interrupted", get_run(history_dir, running_id)["status"])
            self.assertEqual("completed", get_run(history_dir, completed_id)["status"])

    def test_retention_preserves_active_and_newest_failure(self):
        with tempfile.TemporaryDirectory() as history_dir:
            active = start_run(history_dir, "voicelab")
            old_failed = start_run(history_dir, "voicelab")
            finish_run(history_dir, old_failed, "failed")
            newest_failed = start_run(history_dir, "voicelab")
            finish_run(history_dir, newest_failed, "failed")
            completed = start_run(history_dir, "voicelab")
            finish_run(history_dir, completed, "completed")
            removed = prune_runs(history_dir, max_count=0, max_age_days=0)
            self.assertIsNotNone(get_run(history_dir, active))
            self.assertIsNotNone(get_run(history_dir, newest_failed))
            self.assertIn(old_failed, removed)
            self.assertIn(completed, removed)


if __name__ == "__main__":
    unittest.main()
