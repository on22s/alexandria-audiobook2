"""A batch worker that raises must fail its own book, keep the other books
running to completion, and report every worker's failure (#2271)."""
import asyncio
import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

import core
from routers import script, scripts_library
from utils import file_lock


class BatchScriptWorkerFailureTests(unittest.TestCase):
    def _dispatch(self, names, stream, file_lock_factory=None):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            source = data / "active.json"
            source.write_text('[{"text":"manual save"}]')
            for name in names:
                (root / name).write_text("A short source story.")
            api_logs = root / "api-logs"
            api_logs.mkdir()
            for owner, name, value in (
                    (scripts_library, "DATA_DIR", str(data)),
                    (scripts_library, "SCRIPTS_DIR", str(root)),
                    (scripts_library, "SCRIPT_PATH", str(source)),
                    (scripts_library, "VOICE_CONFIG_PATH", str(data / "absent-voices.json"))):
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(
                scripts_library, "get_active_book_id", return_value="book-id"))
            stack.enter_context(patch.object(
                scripts_library, "_saved_book_meta_path",
                side_effect=lambda name: str(root / f"{name}.meta.json")))
            states = copy.deepcopy(core.process_state)
            for value in states.values():
                value["running"] = False
            for owner, name, value in (
                    (core, "process_state", states),
                    (script, "process_state", states),
                    (core, "_task_claims", {}),
                    (core, "_gpu_leases", {}),
                    (script, "UPLOADS_DIR", str(root)),
                    (script, "SCRIPTS_DIR", str(root)),
                    (script, "DATA_DIR", str(data)),
                    (core, "RUN_HISTORY_DIR", str(root / "run_history")),
                    (core, "API_LOG_DIR", str(api_logs))):
                stack.enter_context(patch.object(owner, name, value))
            if file_lock_factory is not None:
                stack.enter_context(
                    patch.object(script, "file_lock", file_lock_factory))
            stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
            stack.enter_context(patch.object(script, "three_pass_refusal", return_value=None))
            stack.enter_context(patch.object(script, "load_app_config", return_value={}))
            stack.enter_context(patch.object(script, "get_active_reasoning_effort",
                                             return_value=None))
            stack.enter_context(patch.object(
                script, "_get_batch_script_workers", return_value=(2, 100, 4096)))
            stack.enter_context(patch.object(
                script, "ensure_ideal_settings", return_value=(True, {}, "CPU test provider")))
            streamed = stack.enter_context(
                patch.object(script, "_stream_subprocess_to_logs", side_effect=stream))
            app = FastAPI()
            app.include_router(scripts_library.router)
            stack.enter_context(TestClient(app))
            tasks = BackgroundTasks()
            request = script.BatchScriptRequest(
                tasks=[script.BatchScriptTask(filename=name) for name in names])
            try:
                response = asyncio.run(script.generate_script_batch_start(request, tasks))
                self.assertEqual("started", response["status"])
                asyncio.run(tasks())
                runs = [json.loads(path.read_text())
                        for path in (root / "run_history").glob("*.json")]
                return states["batch_script"], streamed, runs
            finally:
                core.release_pending_task_claims()

    def test_raising_worker_fails_its_book_and_every_failure_is_logged(self):
        def stream(command, *args, **kwargs):
            if "--output" in command:
                output = command[command.index("--output") + 1]
                if "book-1" in output:
                    raise RuntimeError("provider failed for book-1")
                if "book-2" in output:
                    raise RuntimeError("provider failed for book-2")
                Path(output).write_text('[{"text":"generated"}]')
            return 0, []

        state, streamed, runs = self._dispatch(
            ["book-1.txt", "book-2.txt", "book-3.txt"], stream)

        statuses = [task["status"] for task in state["tasks"]]
        self.assertEqual(["failed", "failed", "done"], statuses)
        self.assertFalse(state["running"])
        logs = "\n".join(state["logs"])
        self.assertIn("provider failed for book-1", logs)
        self.assertIn("provider failed for book-2", logs)
        self.assertNotIn("Batch script generation finished.", logs)
        # One run is recorded as failed with the error, and the book that
        # succeeded stays "done" instead of the run claiming success (#2271).
        self.assertEqual(1, len(runs))
        self.assertEqual("failed", runs[0]["status"])
        self.assertIn("provider failed", runs[0]["error"])
        self.assertEqual("done", state["tasks"][2]["status"])

    def test_cancel_with_a_real_exception_logs_it_and_agrees_with_history(self):
        def stream(command, *args, **kwargs):
            if "--output" in command:
                core.process_state["batch_script"]["cancel"] = True
                raise RuntimeError("provider failed during cancel")
            return 0, []

        state, _, runs = self._dispatch(["book-1.txt"], stream)

        # A real failure is not erased by an in-flight cancel: the row and the
        # run history both say failed, and the error reaches the log (#2271).
        self.assertEqual(["failed"], [task["status"] for task in state["tasks"]])
        self.assertIn("provider failed during cancel", "\n".join(state["logs"]))
        self.assertEqual(1, len(runs))
        self.assertEqual("failed", runs[0]["status"])
        self.assertIn("provider failed during cancel", runs[0]["error"])

    def test_cleanup_failure_after_publish_keeps_the_done_row_and_reports_it(self):
        def stream(command, *args, **kwargs):
            if "--output" in command:
                Path(command[command.index("--output") + 1]).write_text(
                    '[{"text":"generated"}]')
            return 0, []

        @contextlib.contextmanager
        def failing_unlock(path, *args, **kwargs):
            with file_lock(path, *args, **kwargs):
                yield
            raise OSError("cleanup unlock failed")

        state, _, runs = self._dispatch(
            ["book-1.txt"], stream, file_lock_factory=failing_unlock)

        # The book was generated and saved before the cleanup error, so its row
        # stays "done"; the cleanup error is neither hidden from the log nor
        # from the run history (#2271).
        self.assertEqual(["done"], [task["status"] for task in state["tasks"]])
        self.assertIn("Cleanup failed after 'book-1' was saved: cleanup unlock failed",
                      "\n".join(state["logs"]))
        self.assertEqual(1, len(runs))
        self.assertEqual("failed", runs[0]["status"])
        self.assertIn("cleanup unlock failed", runs[0]["error"])

    def test_all_success_still_reports_finished(self):
        def stream(command, *args, **kwargs):
            if "--output" in command:
                Path(command[command.index("--output") + 1]).write_text(
                    '[{"text":"generated"}]')
            return 0, []

        state, _, runs = self._dispatch(["book-1.txt", "book-2.txt"], stream)

        self.assertEqual(["done", "done"], [task["status"] for task in state["tasks"]])
        self.assertFalse(state["running"])
        self.assertIn("Batch script generation finished.", "\n".join(state["logs"]))
        self.assertEqual(["completed"], [run["status"] for run in runs])


if __name__ == "__main__":
    unittest.main()
