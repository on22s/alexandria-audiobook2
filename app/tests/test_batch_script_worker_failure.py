"""A batch worker that raises must fail its own book, keep the other books
running to completion, and report every worker's failure (#2271)."""
import asyncio
import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

import core
from routers import script, scripts_library


class BatchScriptWorkerFailureTests(unittest.TestCase):
    def _dispatch(self, names, stream):
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
                return states["batch_script"], streamed
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

        state, streamed = self._dispatch(["book-1.txt", "book-2.txt", "book-3.txt"], stream)

        statuses = [task["status"] for task in state["tasks"]]
        self.assertEqual(["failed", "failed", "done"], statuses)
        self.assertFalse(state["running"])
        logs = "\n".join(state["logs"])
        self.assertIn("provider failed for book-1", logs)
        self.assertIn("provider failed for book-2", logs)
        self.assertNotIn("Batch script generation finished.", logs)

    def test_all_success_still_reports_finished(self):
        def stream(command, *args, **kwargs):
            if "--output" in command:
                Path(command[command.index("--output") + 1]).write_text(
                    '[{"text":"generated"}]')
            return 0, []

        state, _ = self._dispatch(["book-1.txt", "book-2.txt"], stream)

        self.assertEqual(["done", "done"], [task["status"] for task in state["tasks"]])
        self.assertFalse(state["running"])
        self.assertIn("Batch script generation finished.", "\n".join(state["logs"]))


if __name__ == "__main__":
    unittest.main()
