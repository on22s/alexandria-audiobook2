import json
import os
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from routers import script
import three_pass_generate as tp


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
                 patch.object(script, "build_three_pass_request_preflight",
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

    def test_status_carries_the_pipelines_eta_while_running(self):
        """The Script tab polls /api/status/<task>; the estimate rides along so
        it needs no second request. None once the task is over."""
        state = script.process_state["script"]
        original = dict(state)
        try:
            state.update({"running": True, "start_time": 1.0, "logs": [
                "ETA: about 2m left (Step 2 of 3, 5 of 9 model calls done) [eta_seconds=120 fraction=0.556]"]})
            running = asyncio.run(script.get_status("script"))
            state["running"] = False
            done = asyncio.run(script.get_status("script"))
        finally:
            state.clear()
            state.update(original)
        self.assertEqual(120.0, running["eta"]["eta_seconds"])
        self.assertIn("5 of 9 model calls", running["eta"]["progress"])
        self.assertIsNone(done["eta"])

    def test_preflight_uses_planned_runtime_profile(self):
        report = self._preflight(32768, 2, 9441)
        self.assertEqual(32768, report["context_length"])
        self.assertEqual(16384, report["per_slot_context"])

    def test_batch_source_rejects_unattested_narrator_before_gpu_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Alexis entered. Alexis spoke.", encoding="utf-8")
            job = {"filename": "book.txt", "input_path": str(path),
                   "first_person_narrator": "ALEXIS"}

            with self.assertRaisesRegex(ValueError, "at least three times"):
                script._read_and_validate_batch_script_source(job)

    def test_batch_source_accepts_attested_narrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(
                "Alexis entered. Alexis spoke. Alexis left.", encoding="utf-8")
            job = {"filename": "book.txt", "input_path": str(path),
                   "first_person_narrator": "ALEXIS"}

            text, normalization_count = (
                script._read_and_validate_batch_script_source(job))

        self.assertIn("Alexis left", text)
        self.assertEqual([], normalization_count)

    def test_preflight_uses_three_pass_model_profile_settings(self):
        observed = {}

        def estimate(text, settings, context, parallel, context_windows=None):
            observed.update(settings)
            return {"chunk_count": 1, "worst_predicted_tokens": 1000,
                    "p95_predicted_tokens": 1000,
                    "average_predicted_tokens": 1000.0}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Book text.", encoding="utf-8")
            jobs = [{"filename": "book.txt", "input_path": str(path)}]
            config = {
                "llm": {"model_name": "model"},
                "generation": {
                    "three_pass_chunk_size": 4000,
                    "three_pass_segmentation": "auto",
                    "three_pass_model_profiles": {
                        "model": {"chunk_size": 2200,
                                  "segmentation": "llm"},
                    },
                },
            }
            with patch.object(script, "load_app_config", return_value=config), \
                 patch.object(script, "get_planned_ideal_settings", return_value={
                     "context_length": 32768, "parallel": 1}), \
                 patch.object(script, "build_three_pass_request_preflight",
                              side_effect=estimate):
                script.build_batch_script_preflight(jobs)

        self.assertEqual(2200, observed["chunk_size"])
        self.assertEqual("llm", observed["segmentation"])

    def test_three_pass_estimator_covers_each_llm_stage(self):
        settings = {
            "chunk_size": 6000, "max_tokens": 4096,
            "segment_output_ratio": 3.0, "segmentation": "auto",
        }
        report = tp.build_three_pass_request_preflight(
            'Narration. "Spoken words." More narration.', settings,
            context_length=32768, parallel=2)

        stages = {request["stage"] for request in report["requests"]}
        self.assertIn("attribute", stages)
        self.assertIn("instruct", stages)
        self.assertEqual(16384, report["per_slot_context"])

    def test_three_pass_estimator_includes_context_rescue_for_unknown_split(self):
        settings = {
            "chunk_size": 6000, "max_tokens": 4096,
            "segment_output_ratio": 3.0, "segmentation": "auto",
        }
        report = tp.build_three_pass_request_preflight(
            "Unquoted source text.", settings, context_length=32768,
            parallel=1, context_windows=[2000, 4000])

        stages = [request["stage"] for request in report["requests"]]
        self.assertIn("segment", stages)
        self.assertIn("segment_context_rescue", stages)


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


class OutputCeilingRefusalTests(unittest.TestCase):
    """A pass-1 chunk the model can never re-emit within its output ceiling is
    refused before the run starts, for single and batch alike, with the chunk
    size that would fit; a chunk that fits is not."""

    def _refusal(self, chunk_size, words=12000):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(" ".join(["word"] * words), encoding="utf-8")
            jobs = [{"filename": "book.txt", "input_path": str(path)}]
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"},
                    "generation": {"three_pass_chunk_size": chunk_size, "max_tokens": 4096},
                    "prompts": {}}):
                return script.three_pass_refusal(jobs)

    def test_too_large_a_chunk_is_refused_with_a_suggested_size(self):
        message = self._refusal(30000)
        self.assertIsNotNone(message)
        self.assertIn("30000", message)
        self.assertIn("16384", message)
        self.assertRegex(message, r"Set it to \d+ or below")

    def test_a_chunk_that_fits_is_not_refused(self):
        self.assertIsNone(self._refusal(3000))

    def _quotes_refusal(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(text, encoding="utf-8")
            jobs = [{"filename": "book.txt", "input_path": str(path)}]
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"},
                    "generation": {"three_pass_chunk_size": 3000, "max_tokens": 4096,
                                   "three_pass_segmentation": "quotes"},
                    "prompts": {}}):
                return script.three_pass_refusal(jobs)

    def test_quotes_only_refuses_a_book_that_does_not_mark_dialogue(self):
        """Em-dash dialogue: not one quote mark. Narrating the whole book would
        look like a result; the refusal names the count instead."""
        text = "\n\n".join(["— Where are you going? — she asked. " * 40] * 8)
        message = self._quotes_refusal(text)
        self.assertIsNotNone(message)
        self.assertIn("Quote marks only", message)
        self.assertRegex(message, r"\d+ of \d+ pieces of book.txt contain no quote marks")

    def test_quotes_only_admits_a_quote_marked_book(self):
        text = "\n\n".join(['"Where are you going?" she asked. ' * 40] * 8)
        self.assertIsNone(self._quotes_refusal(text))

    def test_the_output_ceiling_refusal_comes_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(" ".join(["word"] * 12000), encoding="utf-8")
            jobs = [{"filename": "book.txt", "input_path": str(path)}]
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"},
                    "generation": {"three_pass_chunk_size": 30000, "max_tokens": 4096,
                                   "three_pass_segmentation": "quotes"},
                    "prompts": {}}):
                message = script.three_pass_refusal(jobs)
        self.assertIn("write back in one reply", message)

    def test_snapshot_saves_the_finished_prefix_while_the_run_continues(self):
        """#600: the completed prefix (entries with a speaker AND a delivery
        note) goes to the library under the given name with the voice config
        beside it; the run is not touched; nothing finished -> 409; not
        running -> 409."""
        seg = [{"type": "NARRATOR", "text": f"line {i}"} for i in range(5)]
        named = [{"speaker": "NARRATOR", "text": f"line {i}"} for i in range(4)] + [None]
        annotated = [{"speaker": "NARRATOR", "text": f"line {i}", "instruct": "calm"} for i in range(3)] + [None, None]
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "annotated_script.json")
            Path(script.three_pass_checkpoint_path(script_path)).write_text(json.dumps(
                {"stage": "instruct", "chunks_done": 2, "segmented": seg, "named": named, "annotated": annotated}),
                encoding="utf-8")
            Path(tmp, "voice_config.json").write_text('{"NARRATOR": {"type": "custom"}}', encoding="utf-8")
            scripts_dir = os.path.join(tmp, "scripts")
            state = script.process_state["script"]; original = dict(state)
            try:
                state.update({"running": True})
                with patch.object(script, "SCRIPT_PATH", script_path), \
                     patch.object(script, "VOICE_CONFIG_PATH", os.path.join(tmp, "voice_config.json")), \
                     patch.object(script, "SCRIPTS_DIR", scripts_dir), \
                     patch.object(script, "get_active_book_id", return_value="book"), \
                     patch.object(script, "_saved_book_meta_path", lambda name: os.path.join(scripts_dir, f"{name}.meta.json")):
                    res = asyncio.run(script.snapshot_script(script.SnapshotRequest(name="first forty")))
                    self.assertEqual((3, 5, 2), (res["entries"], res["segmented"], res["chunks_done"]))
                    saved = json.loads(Path(scripts_dir, "first forty.json").read_text(encoding="utf-8"))
                    self.assertEqual(["line 0", "line 1", "line 2"], [e["text"] for e in saved])
                    self.assertTrue(Path(scripts_dir, "first forty.voice_config.json").exists())
                    self.assertEqual(3, json.loads(Path(scripts_dir, "first forty.meta.json").read_text())["snapshot"]["entries"])
                    # nothing finished yet
                    Path(script.three_pass_checkpoint_path(script_path)).write_text(json.dumps(
                        {"stage": "segment", "chunks_done": 1, "segmented": seg, "named": [], "annotated": []}), encoding="utf-8")
                    with self.assertRaises(script.HTTPException) as ctx:
                        asyncio.run(script.snapshot_script(script.SnapshotRequest(name="x")))
                    self.assertEqual(409, ctx.exception.status_code)
                    state["running"] = False
                    with self.assertRaises(script.HTTPException) as ctx:
                        asyncio.run(script.snapshot_script(script.SnapshotRequest(name="x")))
                    self.assertEqual(409, ctx.exception.status_code)
            finally:
                state.clear(); state.update(original)

    def test_start_over_discards_the_checkpoint_and_plain_generate_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text('"Hello," she said. ' * 50, encoding="utf-8")
            script_path = str(Path(tmp, "annotated_script.json"))
            ckpt = Path(script.three_pass_checkpoint_path(script_path))
            manifest = Path(script.three_pass_manifest_path(script_path))
            for start_over, kept in ((False, True), (True, False)):
                ckpt.write_text("{}", encoding="utf-8")
                manifest.write_text("{}", encoding="utf-8")
                with patch.object(script, "SCRIPT_PATH", script_path), \
                     patch.object(script, "DATA_DIR", tmp), \
                     patch.object(script, "load_app_config", return_value={
                         "llm": {"model_name": "model"},
                         "generation": {"three_pass_chunk_size": 3000, "max_tokens": 4096},
                         "prompts": {}}), \
                     patch.object(script, "check_global_gpu_lock"), \
                     patch.object(script, "claim_gpu_task"), \
                     patch.object(script, "build_generate_script_command", return_value=["x"]):
                    tasks = script.BackgroundTasks()
                    script.start_script_generation(
                        tasks, str(path), script.GenerateScriptRequest(start_over=start_over))
                with self.subTest(start_over=start_over):
                    self.assertEqual(kept, ckpt.exists())
                    self.assertEqual(kept, manifest.exists())

    def test_single_book_start_refuses_before_claiming_the_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(" ".join(["word"] * 12000), encoding="utf-8")
            with patch.object(script, "load_app_config", return_value={
                    "llm": {"model_name": "model"},
                    "generation": {"three_pass_chunk_size": 30000, "max_tokens": 4096},
                    "prompts": {}}), \
                 patch.object(script, "check_global_gpu_lock"), \
                 patch.object(script, "claim_gpu_task") as claim:
                with self.assertRaises(script.HTTPException) as ctx:
                    script.start_script_generation(None, str(path), None)
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("write back in one reply", ctx.exception.detail)
        claim.assert_not_called()
