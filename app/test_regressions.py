import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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

    def test_preparer_rejects_unimplemented_skip_before_startup(self):
        config = {
            "audio_filename": "book.wav",
            "output_filename": "dataset.zip",
            "skip_annotation": True,
        }
        with self.assertRaises(app_module.HTTPException) as raised:
            asyncio.run(app_module.preparer_start(
                None, json.dumps(config), None, None
            ))
        self.assertEqual(raised.exception.status_code, 400)

    def test_voice_suggestion_honors_max_lines(self):
        captured = {}
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"Hero": {"adapter_id": "voice", "reason": "fit"}}'
            ))]
        )

        def create(**kwargs):
            captured["prompt"] = kwargs["messages"][1]["content"]
            return response

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        ))
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            with open(script_path, "w", encoding="utf-8") as f:
                json.dump([{"speaker": "Hero", "text": f"distinct line {i}"}
                           for i in range(12)], f)
            with patch.object(app_module, "SCRIPT_PATH", script_path), \
                 patch.object(app_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(app_module, "_build_lora_candidates", return_value=[{
                     "adapter_id": "voice", "name": "Voice", "gender": "unknown",
                     "description": "neutral", "type": "lora",
                 }]), \
                 patch.object(app_module, "_make_llm_client", return_value=(client, "model")):
                app_module._suggest_voices_impl(app_module.SuggestVoicesRequest(max_lines=12))
        for i in range(12):
            self.assertIn(f"distinct line {i}", captured["prompt"])

    def test_selective_enrichment_prompt(self):
        fake_llama = SimpleNamespace(Llama=object, llama_supports_gpu_offload=lambda: True)
        path = Path(__file__).resolve().parent.parent / "llm_enricher.py"
        with patch.dict(sys.modules, {"llama_cpp": fake_llama}):
            spec = importlib.util.spec_from_file_location("test_llm_enricher", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        enricher = module.LLMEnricher.__new__(module.LLMEnricher)
        enricher.fields = ["emotional_tone"]
        prompt = enricher._create_prompt({"text": "hello", "start": 0, "end": 1})
        self.assertIn("emotional_tone", prompt)
        self.assertNotIn("speaker_attribution", prompt)
        self.assertNotIn("narration_style", prompt)
        self.assertEqual(
            enricher._parse_llm_output('{"emotional_tone": "calm"}')["emotional_tone"],
            "calm",
        )

    def test_docker_image_includes_root_runtime_dependencies(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        for required in ("gpu_stats.py", "persona_prompts.txt", "alexandria_alignment.py",
                         "alexandria_preparer_rocm_compatible.py",
                         "llm_enricher.py", "voice_analysis.py", "name_voices.py"):
            self.assertIn(required, dockerfile)

    def test_review_source_mode_is_rejected(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("review_script.py")),
             "--source", "unused.txt"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not implemented", result.stderr)

    def test_lora_cancel_idle_reports_not_running(self):
        with self.assertRaises(app_module.HTTPException) as raised:
            asyncio.run(app_module.lora_cancel_training())
        self.assertEqual(raised.exception.status_code, 400)

    def test_frontend_wires_preparer_duration_and_lora_cancel(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("min_chunk_duration: getNumFieldValue('prep-min-chunk-duration', 2)", html)
        self.assertIn("id=\"btn-lora-cancel\"", html)
        self.assertIn("/api/lora/train/cancel", html)
        self.assertNotIn("id=\"prep-skip-annotation\"", html)


if __name__ == "__main__":
    unittest.main()
