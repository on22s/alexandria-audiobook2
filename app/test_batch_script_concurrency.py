import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from routers import script


class BatchScriptConcurrencyTests(unittest.TestCase):
    def _preflight(self, context, parallel, worst):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Book text.", encoding="utf-8")
            jobs = [{"filename": "book-1.txt", "input_path": str(path)},
                    {"filename": "book-2.txt", "input_path": str(path)}]
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"}, "generation": {}, "prompts": {}}), \
                 patch.object(script, "get_planned_ideal_settings", return_value={
                    "context_length": context, "parallel": parallel}), \
                 patch.object(script, "build_book_request_preflight",
                    return_value={"chunk_count": 1, "worst_predicted_tokens": worst,
                                  "p95_predicted_tokens": worst,
                                  "average_predicted_tokens": float(worst)}):
                return script.build_batch_script_preflight(jobs)

    def test_two_workers_when_every_book_fits(self):
        report = self._preflight(32768, 2, 9000)
        self.assertEqual(2, report["workers"])
        self.assertEqual(16384, report["per_slot_context"])
        self.assertIsNone(report["fallback_reason"])
        self.assertTrue(all(book["fits_selected_slot"] for book in report["books"]))

    def test_serializes_when_per_slot_context_is_too_small(self):
        report = self._preflight(16384, 2, 9000)
        self.assertEqual(1, report["workers"])
        self.assertEqual(16384, report["per_slot_context"])
        self.assertIn("Reduced concurrency", report["fallback_reason"])

    def test_worker_compatibility_helper_uses_shared_report(self):
        expected = {"workers": 2, "worst_request_tokens": 9000, "context_length": 32768}
        with patch.object(script, "build_batch_script_preflight", return_value=expected):
            self.assertEqual((2, 9000, 32768), script._get_batch_script_workers([]))

    def test_status_omits_all_live_process_objects(self):
        state = script.process_state["batch_script"]
        original = dict(state)
        try:
            state.update({"running": True, "process": object(),
                          "processes": [object()], "tasks": []})
            public = asyncio.run(script.get_status("batch_script"))
        finally:
            state.clear()
            state.update(original)

        self.assertNotIn("process", public)
        self.assertNotIn("processes", public)

    def test_preflight_uses_planned_runtime_profile(self):
        report = self._preflight(32768, 2, 9441)
        self.assertEqual(32768, report["context_length"])
        self.assertEqual(16384, report["per_slot_context"])


class ResolveBatchOutputPathTests(unittest.TestCase):
    """Covers the Area 6 fix: a `replace`-policy collision with a *reserved*
    (in-batch) output must never share a path with the job that reserved it."""

    def test_no_collision_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "book.json"))
            resolved, action = script._resolve_batch_output_path(path, "replace", set())
            self.assertEqual(path, resolved)
            self.assertEqual("ok", action)

    def test_cancel_policy_skips_on_reserved_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "book.json"))
            reserved = {path}
            resolved, action = script._resolve_batch_output_path(path, "cancel", reserved)
            self.assertEqual(path, resolved)
            self.assertEqual("skip", action)

    def test_version_policy_suffixes_on_reserved_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "book.json"))
            reserved = {path}
            resolved, action = script._resolve_batch_output_path(path, "version", reserved)
            self.assertEqual("version", action)
            self.assertEqual(str(Path(tmp, "book_2.json")), resolved)

    def test_replace_policy_versions_reserved_collision_instead_of_overwriting(self):
        # Two same-stem inputs under collision_policy="replace": task 2's collision
        # is with task 1's *reserved* output, not a disk file. Prior behavior fell
        # through and returned the same path, so task 2 would silently overwrite
        # task 1's output once both jobs ran.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "book.json"))
            reserved = {path}
            resolved, action = script._resolve_batch_output_path(path, "replace", reserved)
            self.assertEqual("version", action)
            self.assertNotEqual(path, resolved)
            self.assertEqual(str(Path(tmp, "book_2.json")), resolved)

    def test_replace_policy_backs_up_disk_only_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "book.json"))
            Path(path).write_text("{}", encoding="utf-8")
            resolved, action = script._resolve_batch_output_path(path, "replace", set())
            self.assertEqual(path, resolved)
            self.assertEqual("backup", action)


if __name__ == "__main__":
    unittest.main()
