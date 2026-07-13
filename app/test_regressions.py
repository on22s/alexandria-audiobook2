import asyncio
import base64
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
import hf_utils
from lmstudio_settings import get_effective_max_tokens, TokenBudgetError


class _Upload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class RegressionTests(unittest.TestCase):
    def test_runtime_data_dir_ignores_empty_environment_override(self):
        with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": "   "}):
            self.assertEqual(utils.get_runtime_data_dir("/expected/root"),
                             "/expected/root")

    def test_voice_design_claims_gpu_before_engine_initialization(self):
        app_module.process_state["audio"]["running"] = True
        try:
            with patch.object(app_module.project_manager, "get_engine") as get_engine:
                with self.assertRaises(app_module.HTTPException) as raised:
                    asyncio.run(app_module.voice_design_preview(
                        app_module.VoiceDesignPreviewRequest(
                            description="voice", sample_text="text", language="english")))
            self.assertEqual(raised.exception.status_code, 400)
            get_engine.assert_not_called()
        finally:
            app_module.process_state["audio"]["running"] = False

    def test_lora_epochs_must_be_positive_at_api_boundary(self):
        for epochs in (0, -1):
            with self.assertRaises(ValueError):
                app_module.LoraTrainingRequest(name="x", dataset_id="d", epochs=epochs)

    def test_builtin_manifest_normalization_skips_bad_entries(self):
        entries = hf_utils._normalize_manifest_entries([
            "bad", {}, {"id": " good ", "name": 42, "final_loss": "bad"}
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "good")
        self.assertEqual(entries[0]["name"], "42")
        self.assertIsNone(entries[0]["final_loss"])

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
        self.assertIn("No downloaded voice matched the known gender; fallback used.", html)
        self.assertIn("Existing recurring voice retained despite a trait mismatch.", html)
        self.assertIn("escapeHtml(sugg.trait_evidence)", html)
        self.assertIn("escapeHtml(config.ref_text || '')", html)
        self.assertIn("escapeHtml(config.ref_audio || '')", html)
        self.assertIn("escapeHtml(config.description || '')", html)
        self.assertNotIn("onclick='downloadBuiltinAdapter(${JSON.stringify(m.id)})'", html)

    def test_launcher_contracts_cover_dynamic_ports_failures_and_rocm_constraints(self):
        root = Path(__file__).resolve().parent.parent
        start = (root / "start.js").read_text(encoding="utf-8")
        start_llm = (root / "start_llm.js").read_text(encoding="utf-8")
        install = (root / "install.js").read_text(encoding="utf-8")
        self.assertIn('port: "{{port}}"', start)
        self.assertIn('ALEXANDRIA_PORT: "{{local.port}}"', start)
        self.assertIn('method: "script.return"', start_llm)
        self.assertIn("pytorch-triton-rocm", install)

    def test_readme_api_examples_do_not_reference_removed_routes(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        for removed in ("/api/parse_voices", "/api/lora/generate_dataset",
                        "/api/voice_design/delete/", "/api/status/script_generation"):
            self.assertNotIn(removed, readme)

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
             "character_style": f"style {name}", "reason": "book evidence",
             "character_gender": "unknown", "age_group": "unknown",
             "trait_evidence": "none", "trait_confidence": "unknown"}
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

    def test_casting_enforces_gender_and_prefers_closest_age(self):
        script = [{"speaker": "Old Man", "text": f"He spoke wearily line {i}"} for i in range(30)]
        parsed = {"characters": [{
            "name": "Old Man", "ranked_adapter_ids": ["female_old", "male_adult", "male_old"],
            "character_style": "Weathered and deliberate", "reason": "book evidence",
            "character_gender": "female", "age_group": "young_adult", "trait_evidence": "Conflicting LLM evidence",
            "trait_confidence": "high",
        }]}
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(parsed)), finish_reason="stop")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: response)))
        candidates = [
            {"adapter_id": "female_old", "name": "F", "type": "lora", "gender": "female", "age_group": "elderly", "description": ""},
            {"adapter_id": "male_adult", "name": "MA", "type": "lora", "gender": "male", "age_group": "adult", "description": ""},
            {"adapter_id": "male_old", "name": "MO", "type": "lora", "gender": "male", "age_group": "elderly", "description": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps(script), encoding="utf-8")
            with patch.object(app_module, "SCRIPT_PATH", script_path), \
                 patch.object(app_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(app_module, "get_active_book_id", return_value="b1"), \
                 patch.object(app_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                 patch.object(app_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(app_module, "_make_llm_client", return_value=(client, "model")), \
                 patch.object(app_module, "get_current_status", return_value={"context_length": None}):
                result = app_module._suggest_voices_impl(app_module.SuggestVoicesRequest(max_lines=4))
        suggestion = result["suggestions"]["Old Man"]
        self.assertEqual(suggestion["adapter_id"], "male_old")
        self.assertEqual(suggestion["character_gender"], "male")
        self.assertEqual(suggestion["character_age_group"], "elderly")
        self.assertEqual(suggestion["gender_confidence"], "high")
        self.assertEqual(suggestion["age_confidence"], "high")
        self.assertIn("character label: male", suggestion["trait_evidence"])
        self.assertEqual(suggestion["llm_trait_evidence"], "Conflicting LLM evidence")
        self.assertFalse(suggestion["gender_fallback"])

    def test_lora_age_normalization(self):
        self.assertEqual(app_module._infer_lora_age({"id": "warm_baritone_40s_m"}), "middle_aged")
        self.assertEqual(app_module._infer_lora_age({"description": "elderly gravelly bass"}), "elderly")
        self.assertEqual(app_module._infer_lora_age({"age": "40s"}), "middle_aged")
        self.assertEqual(app_module._infer_lora_age({"age": 67}), "elderly")

    def test_numeric_age_boundaries_and_precedence(self):
        expected = {"aged 12": "child", "13 years old": "teen", "19-year-old": "teen",
                    "20 years old": "young_adult", "aged 39": "adult",
                    "40-year-old": "middle_aged", "aged 60": "elderly"}
        for text, group in expected.items():
            self.assertEqual(app_module._infer_age_group(text), group, text)
        self.assertEqual(app_module._infer_age_group("young man, aged 50"), "middle_aged")

    def test_age_parser_handles_invalid_ambiguous_and_decade_values(self):
        expected = {
            "0": "unknown", "1": "child", "67": "elderly", "120": "elderly",
            "121": "unknown", "aged 12 then aged 60": "child", "under 12": "child",
            "20s": "young_adult", "30s": "adult", "40s": "middle_aged",
            "50s": "middle_aged", "60s": "elderly", "70s": "elderly", "80s": "elderly",
        }
        for text, group in expected.items():
            self.assertEqual(app_module._infer_age_group(text), group, text)

    def test_llm_traits_replace_local_traits_only_with_stronger_authority(self):
        candidates = [
            {"adapter_id": "female", "name": "F", "type": "lora", "gender": "female",
             "age_group": "adult", "description": ""},
            {"adapter_id": "male", "name": "M", "type": "lora", "gender": "male",
             "age_group": "adult", "description": ""},
        ]

        def suggest(confidence):
            parsed = {"characters": [{
                "name": "Hero", "ranked_adapter_ids": ["male", "female"],
                "character_style": "Direct", "reason": "book evidence",
                "character_gender": "male", "age_group": "young_adult",
                "trait_evidence": "The text identifies him as male",
                "trait_confidence": confidence,
            }]}
            response = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(parsed)), finish_reason="stop")])
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **_kwargs: response)))
            with tempfile.TemporaryDirectory() as tmp:
                script_path = os.path.join(tmp, "script.json")
                Path(script_path).write_text(json.dumps([
                    {"speaker": "Hero", "text": "She was an old woman who entered quietly."}
                ]), encoding="utf-8")
                with patch.object(app_module, "SCRIPT_PATH", script_path), \
                     patch.object(app_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                     patch.object(app_module, "get_active_book_id", return_value="b1"), \
                     patch.object(app_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                     patch.object(app_module, "_build_lora_candidates", return_value=candidates), \
                     patch.object(app_module, "_make_llm_client", return_value=(client, "model")), \
                     patch.object(app_module, "get_current_status", return_value={"context_length": None}):
                    return app_module._suggest_voices_impl(
                        app_module.SuggestVoicesRequest(max_lines=4))["suggestions"]["Hero"]

        for confidence in ("unknown", "low"):
            suggestion = suggest(confidence)
            self.assertEqual(suggestion["character_gender"], "female", confidence)
            self.assertEqual(suggestion["gender_confidence"], "low", confidence)
            self.assertEqual(suggestion["character_age_group"], "elderly", confidence)
            self.assertEqual(suggestion["age_confidence"], "low", confidence)
        for confidence in ("medium", "high"):
            suggestion = suggest(confidence)
            self.assertEqual(suggestion["character_gender"], "male", confidence)
            self.assertEqual(suggestion["gender_confidence"], confidence)
            self.assertEqual(suggestion["character_age_group"], "young_adult", confidence)
            self.assertEqual(suggestion["age_confidence"], confidence)

    def test_mixed_llm_trait_acceptance_does_not_merge_conflicting_evidence(self):
        parsed = {"characters": [{
            "name": "King", "ranked_adapter_ids": ["male_old"],
            "character_style": "Measured", "reason": "book evidence",
            "character_gender": "female", "age_group": "elderly",
            "trait_evidence": "An elderly woman speaks", "trait_confidence": "high",
        }]}
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(parsed)), finish_reason="stop")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: response)))
        candidates = [{"adapter_id": "male_old", "name": "MO", "type": "lora",
                       "gender": "male", "age_group": "elderly", "description": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps([
                {"speaker": "King", "text": "The crown is mine."}
            ]), encoding="utf-8")
            with patch.object(app_module, "SCRIPT_PATH", script_path), \
                 patch.object(app_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(app_module, "get_active_book_id", return_value="b1"), \
                 patch.object(app_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                 patch.object(app_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(app_module, "_make_llm_client", return_value=(client, "model")), \
                 patch.object(app_module, "get_current_status", return_value={"context_length": None}):
                suggestion = app_module._suggest_voices_impl(
                    app_module.SuggestVoicesRequest(max_lines=4))["suggestions"]["King"]
        self.assertEqual(suggestion["character_gender"], "male")
        self.assertEqual(suggestion["character_age_group"], "elderly")
        self.assertIn("LM accepted age=elderly", suggestion["trait_evidence"])
        self.assertNotIn("elderly woman", suggestion["trait_evidence"])
        self.assertEqual(suggestion["llm_trait_evidence"], "An elderly woman speaks")

    def test_low_confidence_traits_do_not_hard_filter(self):
        candidates = [
            {"adapter_id": "male", "gender": "male", "age_group": "adult", "description": ""},
            {"adapter_id": "female", "gender": "female", "age_group": "elderly", "description": ""},
        ]
        traits = {"gender": "female", "gender_confidence": "low",
                  "age_group": "elderly", "age_confidence": "low"}
        chosen, _ranked, _new, fallback, _mismatch = app_module.get_voice_allocation(
            "", candidates, ["male", "female"], traits, None, {}, "minor")
        self.assertEqual(chosen, "male")
        self.assertFalse(fallback)

    def test_gender_fallback_and_recurring_mismatch(self):
        male = {"adapter_id": "male", "gender": "male", "age_group": "adult", "description": ""}
        traits = {"gender": "female", "gender_confidence": "high",
                  "age_group": "young_adult", "age_confidence": "high"}
        chosen, _ranked, _new, fallback, mismatch = app_module.get_voice_allocation(
            "", [male], ["male"], traits, None, {}, "major")
        self.assertEqual(chosen, "male")
        self.assertTrue(fallback)
        self.assertFalse(mismatch)
        chosen, _ranked, is_new, _fallback, mismatch = app_module.get_voice_allocation(
            "", [male], ["male"], traits, "male", {}, "major")
        self.assertFalse(is_new)
        self.assertTrue(mismatch)

    def test_authoritative_gender_prefers_exact_then_unknown_then_opposite(self):
        female = {"adapter_id": "female", "gender": "female", "age_group": "adult", "description": ""}
        unknown = {"adapter_id": "unknown", "gender": "unknown", "age_group": "adult", "description": ""}
        male = {"adapter_id": "male", "gender": "male", "age_group": "adult", "description": ""}
        traits = {"gender": "female", "gender_confidence": "high",
                  "age_group": "adult", "age_confidence": "high"}
        cases = [
            ([male, unknown, female], "female", False),
            ([male, unknown], "unknown", True),
            ([male], "male", True),
        ]
        for candidates, expected, expected_fallback in cases:
            chosen, _ranked, _new, fallback, _mismatch = app_module.get_voice_allocation(
                "", candidates, [c["adapter_id"] for c in candidates],
                traits, None, {}, "major")
            self.assertEqual(chosen, expected)
            self.assertEqual(fallback, expected_fallback)

    def test_recurring_mismatch_requires_authoritative_trait_confidence(self):
        male = {"adapter_id": "male", "gender": "male", "age_group": "child", "description": ""}
        for confidence in ("unknown", "low", "medium", "high"):
            traits = {"gender": "female", "gender_confidence": confidence,
                      "age_group": "elderly", "age_confidence": confidence}
            _chosen, _ranked, _new, _fallback, mismatch = app_module.get_voice_allocation(
                "", [male], ["male"], traits, "male", {}, "major")
            self.assertEqual(mismatch, confidence in ("medium", "high"), confidence)

    def test_character_trait_evidence_prefers_label_over_dialogue(self):
        traits = app_module._infer_character_traits(
            "Young Man", "", ["She told her mother that the queen had arrived."])
        self.assertEqual(traits["gender"], "male")
        self.assertEqual(traits["gender_confidence"], "high")
        self.assertEqual(traits["age_group"], "young_adult")

    def test_apply_suggestion_persists_style_and_book_scoped_cast_member(self):
        candidate = {"adapter_id": "v1", "name": "V1", "type": "lora",
                     "gender": "unknown", "description": ""}
        suggestion = {"adapter_id": "v1", "character_style": "Brief wary delivery", "book_id": "book-05",
                      "priority": "minor", "reason": "small suspicious role",
                      "character_gender": "male", "character_age_group": "adult",
                      "voice_gender": "male", "voice_age_group": "middle_aged",
                      "gender_confidence": "high", "age_confidence": "medium",
                      "trait_evidence": "label evidence", "local_trait_evidence": "label evidence",
                      "llm_trait_evidence": "", "gender_fallback": False,
                      "existing_trait_mismatch": True}
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
        for field in app_module.get_trait_assignment_metadata(suggestion):
            self.assertEqual(voice["Man"][field], suggestion[field], field)
        member = library["casts"]["series"]["members"]["man::book-05"]
        self.assertEqual(member["config"]["adapter_id"], "v1")
        for field in app_module.get_trait_assignment_metadata(suggestion):
            self.assertEqual(member["config"][field], suggestion[field], field)
            self.assertEqual(member["assignments"]["book-05"][field], suggestion[field], field)
        self.assertEqual(member["assignments"]["book-05"]["character_style"], "Brief wary delivery")
        self.assertEqual(member["assignments"]["book-05"]["character_gender"], "male")
        self.assertEqual(member["assignments"]["book-05"]["age_confidence"], "medium")
        self.assertTrue(member["assignments"]["book-05"]["existing_trait_mismatch"])
        self.assertEqual(result["adapter_usage"]["v1"]["character_count"], 1)

    def test_cast_apply_uses_book_specific_style(self):
        assignment_traits = {
            "character_gender": "female", "character_age_group": "adult",
            "voice_gender": "female", "voice_age_group": "middle_aged",
            "trait_evidence": "book five evidence", "local_trait_evidence": "local evidence",
            "llm_trait_evidence": "LM evidence", "gender_confidence": "high",
            "age_confidence": "medium", "gender_fallback": False,
            "existing_trait_mismatch": True,
        }
        lib = {"shared": {}, "casts": {"series": {"members": {"holo": {
            "name": "Holo", "config": {"type": "lora", "adapter_id": "v1",
                                         "character_style": "default style",
                                         "character_gender": "female",
                                         "character_age_group": "young_adult"},
            "assignments": {"book-05": {"character_style": "book five style",
                                           **assignment_traits}},
        }}}}}
        config, applied = app_module._apply_cast_mapping(
            lib, "series", {"Holo": "holo"}, {}, book_id="book-05")
        self.assertEqual(applied, ["Holo"])
        self.assertEqual(config["Holo"]["character_style"], "book five style")
        self.assertEqual(config["Holo"]["character_age_group"], "adult")
        self.assertEqual(config["Holo"]["age_confidence"], "medium")
        self.assertEqual(config["Holo"]["trait_evidence"], "book five evidence")
        for field, value in assignment_traits.items():
            self.assertEqual(config["Holo"][field], value, field)

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

    def test_runtime_data_dir_isolates_mutable_app_paths(self):
        root = Path(__file__).resolve().parent.parent
        code = (
            "import app; print(app.DATA_DIR); print(app.SCRIPT_PATH); "
            "print(app.VOICE_LIBRARY_PATH); print(app.CONFIG_PATH); print(app.UPLOADS_DIR)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ALEXANDRIA_DATA_DIR=tmp)
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=root / "app", env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
            self.assertTrue(paths)
            for path in paths:
                self.assertTrue(utils.is_path_inside(path, tmp), path)

    def test_docker_mounts_single_persistent_runtime_root(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("ALEXANDRIA_DATA_DIR=/alexandria/runtime", dockerfile)
        self.assertIn("./data/runtime:/alexandria/runtime", compose)

    def test_review_help_does_not_advertise_unimplemented_source_mode(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("review_script.py")),
             "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--source", result.stdout)

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

    def test_check_basic_auth_accepts_and_rejects_credentials(self):
        make = lambda raw: "Basic " + base64.b64encode(raw.encode()).decode()
        # Correct credentials pass.
        self.assertTrue(utils.check_basic_auth(
            make("alexandria:secret"), "alexandria", "secret"))
        # Wrong password and wrong username both fail.
        self.assertFalse(utils.check_basic_auth(
            make("alexandria:wrong"), "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            make("intruder:secret"), "alexandria", "secret"))
        # Malformed / missing inputs fail closed rather than raising.
        self.assertFalse(utils.check_basic_auth(
            make("nocolonhere"), "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            "Basic !!!not-base64!!!", "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth(
            "Bearer " + base64.b64encode(b"alexandria:secret").decode(),
            "alexandria", "secret"))
        self.assertFalse(utils.check_basic_auth("", "alexandria", "secret"))


if __name__ == "__main__":
    unittest.main()
