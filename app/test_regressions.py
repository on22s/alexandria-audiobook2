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
import utils
from lmstudio_settings import get_effective_max_tokens, TokenBudgetError


class _Upload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class RegressionTests(unittest.TestCase):
    def test_token_budget_uses_fallback_without_verified_context(self):
        self.assertEqual(4096, get_effective_max_tokens(4096, None, [], 16000))

    def test_token_budget_scales_with_verified_context(self):
        self.assertEqual(16000, get_effective_max_tokens(4096, 98304, [], 16000))

    def test_token_budget_reserves_prompt_space(self):
        messages = [{"role": "user", "content": "x" * 18000}]
        self.assertEqual(1680, get_effective_max_tokens(4096, 8192, messages, 16000))

    def test_token_budget_enforces_task_ceiling(self):
        self.assertEqual(6000, get_effective_max_tokens(2000, 98304, [], 6000))

    def test_token_budget_rejects_prompt_larger_than_context(self):
        with self.assertRaises(TokenBudgetError):
            get_effective_max_tokens(100, 1000, [{"role": "user", "content": "x" * 3000}], 500)

    def test_token_budget_rejects_invalid_context(self):
        with self.assertRaises(ValueError):
            get_effective_max_tokens(100, "not-a-number", [], 500)

    def test_lora_style_attribute_escapes_persisted_content(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("value=\"${escapeHtml(voiceType === 'lora' ? (config.character_style || '') : '')}\"", html)

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

    def test_generic_cast_keys_are_book_scoped(self):
        self.assertEqual(app_module.get_cast_member_key("Man", "book-01"), "man::book-01")
        self.assertEqual(app_module.get_cast_member_key("Man", "book-05"), "man::book-05")
        self.assertEqual(app_module.get_cast_member_key("Holo", "book-01"), "holo")
        with self.assertRaises(ValueError):
            app_module.get_cast_member_key("Guard", None)

    def test_cast_usage_counts_distinct_members_not_books(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "holo": {"name": "Holo", "config": {"adapter_id": "v1"},
                     "assignments": {"b1": {"line_count": 10}, "b2": {"line_count": 20}}},
            "man::b1": {"name": "Man", "config": {"adapter_id": "v1"},
                        "assignments": {"b1": {"line_count": 3}}},
        }}}}
        usage = app_module.get_cast_adapter_usage(lib, "series")
        self.assertEqual(usage["v1"]["character_count"], 2)
        self.assertEqual(usage["v1"]["total_lines"], 33)

    def test_major_characters_get_distinct_voices_before_minor_reuse(self):
        script = ([{"speaker": "Major A", "text": f"a{i}"} for i in range(30)]
                  + [{"speaker": "Major B", "text": f"b{i}"} for i in range(25)]
                  + [{"speaker": "Man", "text": f"m{i}"} for i in range(3)])
        parsed = {"characters": [
            {"name": name, "ranked_adapter_ids": ["v1", "v2"],
             "character_style": f"style {name}", "reason": "book evidence"}
            for name in ("Major A", "Major B", "Man")
        ]}
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(parsed)))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: response)))
        candidates = [
            {"adapter_id": "v1", "name": "V1", "type": "lora", "gender": "unknown", "description": ""},
            {"adapter_id": "v2", "name": "V2", "type": "lora", "gender": "unknown", "description": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            with open(script_path, "w", encoding="utf-8") as f:
                json.dump(script, f)
            with patch.object(app_module, "SCRIPT_PATH", script_path), \
                 patch.object(app_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(app_module, "get_active_book_id", return_value="book-01"), \
                 patch.object(app_module, "_load_voice_library", return_value={"shared": {}, "casts": {"series": {"members": {}}}}), \
                 patch.object(app_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(app_module, "_make_llm_client", return_value=(client, "model")):
                result = app_module._suggest_voices_impl(
                    app_module.SuggestVoicesRequest(cast="series", max_lines=4))
        self.assertEqual(result["suggestions"]["Major A"]["adapter_id"], "v1")
        self.assertEqual(result["suggestions"]["Major B"]["adapter_id"], "v2")
        self.assertTrue(result["suggestions"]["Man"]["reused"])
        self.assertEqual(list(result["suggestions"])[0], "Major A")
        self.assertEqual(result["method"], "llm")
        self.assertEqual(result["suggestions"]["Major A"]["character_style"], "style Major A")

    def test_apply_suggestion_persists_style_and_book_scoped_cast_member(self):
        candidate = {"adapter_id": "v1", "name": "V1", "type": "lora",
                     "gender": "unknown", "description": ""}
        suggestion = {"adapter_id": "v1", "character_style": "Brief wary delivery", "book_id": "book-05",
                      "priority": "minor", "reason": "small suspicious role"}
        with tempfile.TemporaryDirectory() as tmp:
            voice_path = os.path.join(tmp, "voice_config.json")
            library_path = os.path.join(tmp, "voice_library.json")
            with open(library_path, "w", encoding="utf-8") as f:
                json.dump({"shared": {}, "casts": {"series": {"members": {}}}}, f)
            with patch.object(app_module, "VOICE_CONFIG_PATH", voice_path), \
                 patch.object(app_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(app_module, "get_active_book_id", return_value="book-05"), \
                 patch.object(app_module, "_script_line_counts", return_value={"Man": 4}), \
                 patch.object(app_module, "_build_lora_candidates", return_value=[candidate]):
                result = app_module._apply_voice_suggestions({"Man": suggestion}, "series")
            voice = json.loads(Path(voice_path).read_text(encoding="utf-8"))
            library = json.loads(Path(library_path).read_text(encoding="utf-8"))
        self.assertEqual(voice["Man"]["character_style"], "Brief wary delivery")
        member = library["casts"]["series"]["members"]["man::book-05"]
        self.assertEqual(member["config"]["adapter_id"], "v1")
        self.assertEqual(member["assignments"]["book-05"]["character_style"], "Brief wary delivery")
        self.assertEqual(result["adapter_usage"]["v1"]["character_count"], 1)

    def test_cast_apply_uses_book_specific_style(self):
        lib = {"shared": {}, "casts": {"series": {"members": {"holo": {
            "name": "Holo", "config": {"type": "lora", "adapter_id": "v1",
                                         "character_style": "default style"},
            "assignments": {"book-05": {"character_style": "book five style"}},
        }}}}}
        config, applied = app_module._apply_cast_mapping(
            lib, "series", {"Holo": "holo"}, {}, book_id="book-05")
        self.assertEqual(applied, ["Holo"])
        self.assertEqual(config["Holo"]["character_style"], "book five style")

    def test_legacy_generic_cast_member_is_not_auto_matched(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "man": {"name": "Man", "config": {"type": "lora", "adapter_id": "v1"}}
        }}}}
        self.assertNotIn("man", app_module._cast_match_pool(lib, "series", "book-05"))

    def test_numbered_generic_cast_keys_are_book_scoped(self):
        self.assertEqual(app_module.get_cast_member_key("Man 1", "b5"), "man 1::b5")
        self.assertEqual(app_module.get_cast_member_key("Guard #2", "b5"), "guard #2::b5")

    def test_shared_narrator_is_reused_and_counted(self):
        lib = {"shared": {"narrator": {"name": "Narrator", "config": {"adapter_id": "v1"},
                                         "assignments": {"b1": {"line_count": 100}}}},
               "casts": {"series": {"members": {}}}}
        self.assertEqual(app_module.get_cast_adapter_usage(lib, "series")["v1"]["character_count"], 1)
        self.assertIs(app_module.get_cast_storage_pool(lib, "series", "Narrator"), lib["shared"])

    def test_bulk_generic_mapping_resolves_per_book_member(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "man::b1": {"name": "Man", "config": {"adapter_id": "v1"}, "generic": True,
                         "book_id": "b1", "assignments": {"b1": {"character_style": "one"}}},
            "man::b5": {"name": "Man", "config": {"adapter_id": "v2"}, "generic": True,
                         "book_id": "b5", "assignments": {"b5": {"character_style": "five"}}},
        }}}}
        config, _ = app_module._apply_cast_mapping(
            lib, "series", {"Man": "man::b1"}, {}, chars={"Man": 2}, book_id="b5")
        self.assertEqual(config["Man"]["adapter_id"], "v2")
        self.assertEqual(config["Man"]["character_style"], "five")

    def test_stale_suggestion_is_rejected(self):
        with patch.object(app_module, "_build_lora_candidates", return_value=[]), \
             patch.object(app_module, "_script_line_counts", return_value={"Narrator": 2}), \
             patch.object(app_module, "get_active_book_id", return_value="book-b"):
            with self.assertRaisesRegex(Exception, "different book"):
                app_module._apply_voice_suggestions(
                    {"Narrator": {"adapter_id": "v1", "book_id": "book-a"}}, None)

    def test_pair_write_rolls_back_first_file_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = os.path.join(tmp, "first.json"), os.path.join(tmp, "second.json")
            Path(first).write_text('{"old": 1}', encoding="utf-8")
            Path(second).write_text('{"old": 2}', encoding="utf-8")
            real_replace = os.replace
            def fail_second(src, dst):
                if dst == second and os.path.basename(src).startswith(".pair-"):
                    raise OSError("disk failure")
                return real_replace(src, dst)
            with patch.object(utils.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    utils.atomic_json_write_pair({"new": 1}, first, {"new": 2}, second)
            self.assertEqual(json.loads(Path(first).read_text()), {"old": 1})
            self.assertEqual(json.loads(Path(second).read_text()), {"old": 2})

    def test_saved_book_metadata_preserves_original_identity(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app_module, "SCRIPTS_DIR", tmp):
            utils.atomic_json_write({"book_id": "original-upload"},
                                    app_module._saved_book_meta_path("volume-1"))
            self.assertEqual(app_module._get_saved_book_id("volume-1"), "original-upload")

    def test_saved_book_metadata_is_not_listed_as_a_script(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app_module, "SCRIPTS_DIR", tmp):
            Path(tmp, "volume-1.json").write_text("[]", encoding="utf-8")
            Path(tmp, "volume-1.meta.json").write_text('{"book_id":"original"}', encoding="utf-8")
            listed = asyncio.run(app_module.list_saved_scripts())
        self.assertEqual([item["name"] for item in listed], ["volume-1"])

    def test_apply_narrator_suggestion_uses_shared_pool(self):
        candidate = {"adapter_id": "v1", "name": "V1", "type": "lora",
                     "gender": "unknown", "description": ""}
        suggestion = {"adapter_id": "v1", "book_id": "b1",
                      "character_style": "steady", "priority": "major"}
        with tempfile.TemporaryDirectory() as tmp:
            voice_path = os.path.join(tmp, "voice.json")
            library_path = os.path.join(tmp, "library.json")
            Path(library_path).write_text(
                json.dumps({"shared": {}, "casts": {"series": {"members": {}}}}), encoding="utf-8")
            with patch.object(app_module, "VOICE_CONFIG_PATH", voice_path), \
                 patch.object(app_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(app_module, "get_active_book_id", return_value="b1"), \
                 patch.object(app_module, "_script_line_counts", return_value={"Narrator": 100}), \
                 patch.object(app_module, "_build_lora_candidates", return_value=[candidate]):
                app_module._apply_voice_suggestions({"Narrator": suggestion}, "series")
            library = json.loads(Path(library_path).read_text(encoding="utf-8"))
        self.assertEqual(library["shared"]["narrator"]["config"]["adapter_id"], "v1")
        self.assertNotIn("narrator", library["casts"]["series"]["members"])

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
