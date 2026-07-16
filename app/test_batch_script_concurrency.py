import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routers import script


class BatchScriptConcurrencyTests(unittest.TestCase):
    def _workers(self, context, parallel, worst):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Book text.", encoding="utf-8")
            jobs = [{"input_path": str(path)}, {"input_path": str(path)}]
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"}, "generation": {}, "prompts": {}}), \
                 patch.object(script, "get_lmstudio_status", return_value={
                    "context_length": context, "parallel": parallel}), \
                 patch.object(script, "build_book_request_preflight",
                    return_value={"worst_predicted_tokens": worst}):
                return script._get_batch_script_workers(jobs)

    def test_two_workers_when_every_book_fits(self):
        self.assertEqual(2, self._workers(32768, 2, 9000)[0])

    def test_serializes_when_per_slot_context_is_too_small(self):
        self.assertEqual(1, self._workers(16384, 2, 9000)[0])


if __name__ == "__main__":
    unittest.main()
