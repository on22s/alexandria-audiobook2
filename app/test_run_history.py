import tempfile
import unittest
from unittest.mock import Mock, patch

import core
from run_history import finish_run, get_run, list_runs, start_run


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


if __name__ == "__main__":
    unittest.main()
