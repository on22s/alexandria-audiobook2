import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
import generate_script


class _Upload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class RegressionTests(unittest.TestCase):
    def test_queued_cancel_survives_background_start(self):
        key = "_test_claimed_task"
        app_module.process_state[key] = {
            "running": False, "cancel": True, "logs": [], "process": None,
        }
        app_module.GPU_TASKS.add(key)
        try:
            app_module.claim_gpu_task(key)
            self.assertFalse(app_module.process_state[key]["cancel"])
            app_module.process_state[key]["cancel"] = True
            observed = []
            app_module._run_claimed_background_task(
                key, lambda: observed.append(app_module.process_state[key]["cancel"])
            )
            self.assertEqual(observed, [True])
            self.assertFalse(app_module.process_state[key]["running"])
        finally:
            app_module.GPU_TASKS.discard(key)
            app_module.process_state.pop(key, None)

    def test_failed_claimed_task_releases_running_state(self):
        key = "_test_failed_task"
        app_module.process_state[key] = {"running": True, "logs": [], "process": None}
        try:
            app_module._run_claimed_background_task(
                key, lambda: (_ for _ in ()).throw(OSError("launch failed"))
            )
            self.assertFalse(app_module.process_state[key]["running"])
            self.assertIn("launch failed", app_module.process_state[key]["logs"][-1])
        finally:
            app_module.process_state.pop(key, None)

    def test_oversized_upload_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "upload.bin")
            with self.assertRaises(app_module.HTTPException) as raised:
                asyncio.run(app_module._save_upload_limited(
                    _Upload([b"1234", b"5678"]), path, 6
                ))
            self.assertEqual(raised.exception.status_code, 413)
            self.assertFalse(os.path.exists(path))

    def test_llm_salvage_waits_until_retries_are_exhausted(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop"
            )],
            usage=None,
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: response)
            )
        )
        params = generate_script.LLMGenParams("system", "{text}", 100, 0.1, 1, 0, 0, 0, "")
        complete = [{"type": "narration", "text": "complete"}]
        with patch.object(generate_script, "clean_json_string", return_value="[]"), \
             patch.object(generate_script, "repair_json_array", side_effect=[[], complete]), \
             patch.object(generate_script, "salvage_json_entries", return_value=[{"text": "partial"}]) as salvage:
            result = generate_script.call_llm_for_entries(
                client, "model", "system", "text", params,
                "test_responses.log", "TEST", max_retries=1
            )
        self.assertEqual(result, complete)
        salvage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
