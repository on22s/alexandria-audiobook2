import importlib.util
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core as core_module
import tts as tts_module
from routers import voice_library as voice_library_module
from routers import voices as voices_module


class VoicesTests(unittest.TestCase):
    def setUp(self):
        state = copy.deepcopy(core_module.process_state)
        for value in state.values():
            value['running'] = False
        for owner, name, value in ((core_module, 'process_state', state),
                                  (voices_module, 'process_state', state),
                                  (core_module, '_task_claims', {}),
                                  (core_module, '_gpu_leases', {})):
            context = patch.object(owner, name, value)
            context.start()
            self.addCleanup(context.stop)
        for context in (patch.object(core_module, 'acquire_gpu_lock', return_value=None),
                        patch.object(core_module, 'llm_is_on_this_gpu', return_value=True)):
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(core_module.release_pending_task_claims)

    def test_persona_recovery_requires_integrity_fields(self):
        with self.assertRaises(voices_module.HTTPException) as missing:
            voices_module._validate_persona_recovery('{"description":"only"}')
        self.assertEqual(422, missing.exception.status_code)
        self.assertEqual(
            ("warm and measured", "Hello there."),
            voices_module._validate_persona_recovery(
                'prefix {"description":"warm and measured", "ref_text":"Hello there."} suffix'))

    def test_persona_recovery_saves_without_clobbering_existing_voice_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            config_path = os.path.join(tmp, "voice_config.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            Path(config_path).write_text(json.dumps({"Hero": {"type": "custom", "voice": "Ryan"}}), encoding="utf-8")
            request = voices_module.PersonaRecoveryRequest(
                speaker="Hero", persona_json=json.dumps({"description": "steady", "ref_text": "I am ready."}))
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", config_path):
                result = asyncio.run(voices_module.recover_persona(voices_module.BackgroundTasks(), request))
            saved = json.loads(Path(config_path).read_text(encoding="utf-8"))
            self.assertEqual({"status": "saved", "speaker": "Hero"}, result)
            self.assertEqual("custom", saved["Hero"]["type"])
            self.assertEqual("steady", saved["Hero"]["description"])
            self.assertEqual("I am ready.", saved["Hero"]["ref_text"])

    def test_persona_recovery_rejects_unknown_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path):
                with self.assertRaises(voices_module.HTTPException) as error:
                    asyncio.run(voices_module.recover_persona(voices_module.BackgroundTasks(), voices_module.PersonaRecoveryRequest(
                        speaker="Typo", persona_json='{"description":"steady", "ref_text":"I am ready."}')))
            self.assertEqual(422, error.exception.status_code)

    def test_persona_recovery_resume_queues_single_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            config_path = os.path.join(tmp, "voice_config.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            tasks = voices_module.BackgroundTasks()
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", config_path), \
                 patch.object(voices_module, "check_global_gpu_lock"), \
                 patch.object(core_module, "claim_gpu_task", wraps=core_module.claim_gpu_task), \
                 patch.object(voices_module, "run_process"):
                result = asyncio.run(voices_module.recover_persona(tasks, voices_module.PersonaRecoveryRequest(
                    speaker="Hero", resume=True,
                    persona_json='{"description":"steady", "ref_text":"I am ready."}')))
            self.assertEqual("resuming", result["status"])
            self.assertEqual(1, len(tasks.tasks))
            self.assertIn("--recovered-speaker", tasks.tasks[0].args[2].args[0])

    def test_dynamic_narrator_strategy_resolves_focus_voice(self):
        narrator = {"type": "custom", "voice": "Ryan", "narrator_strategy": "focus"}
        config = {"NARRATOR": narrator, "HERO": {"type": "custom", "voice": "Serena"}}
        resolved = tts_module.resolve_narrator_voice_config(
            "NARRATOR", config, {"focus_speaker": "HERO"})
        self.assertEqual("Serena", resolved["NARRATOR"]["voice"])
        self.assertEqual("Ryan", config["NARRATOR"]["voice"])

    def test_dynamic_narrator_strategy_resolves_chapter_version(self):
        config = {"NARRATOR": {"type": "custom", "voice": "Ryan",
                                "narrator_strategy": "chapter",
                                "versions": {"battle": {"type": "custom", "voice": "Dylan"}}}}
        resolved = tts_module.resolve_narrator_voice_config(
            "NARRATOR", config, {"narrator_version": "battle"})
        self.assertEqual("Dylan", resolved["NARRATOR"]["voice"])

    def test_dynamic_narrator_strategy_accepts_title_case_narrator(self):
        config = {"Narrator": {"type": "custom", "voice": "Ryan", "narrator_strategy": "chapter",
                                "versions": {"teen": {"type": "custom", "voice": "Dylan", "age_group": "teen"}}}}
        resolved = tts_module.resolve_narrator_voice_config(
            "Narrator", config, {"narrator_version": "teen"})
        self.assertEqual("Dylan", resolved["NARRATOR"]["voice"])

    def test_dynamic_narrator_combined_strategy_honors_age_and_gender(self):
        config = {
            "NARRATOR": {"type": "custom", "voice": "Ryan", "narrator_strategy": "character_gender_age"},
            "ALICE": {"type": "custom", "voice": "Dylan", "gender": "female", "age_group": "teen"},
        }
        resolved = tts_module.resolve_narrator_voice_config(
            "NARRATOR", config, {"focus_speaker": "ALICE", "focus_gender": "male", "focus_age_group": "teen"})
        self.assertEqual("Ryan", resolved["NARRATOR"]["voice"])

    def test_dynamic_narrator_strategy_matches_gender_version(self):
        config = {"NARRATOR": {"voice": "Ryan", "narrator_strategy": "gender",
                                "versions": {"f": {"type": "custom", "voice": "Serena", "gender": "female"}}}}
        resolved = tts_module.resolve_narrator_voice_config(
            "NARRATOR", config, {"narrator_gender": "female"})
        self.assertEqual("Serena", resolved["NARRATOR"]["voice"])


    def test_empty_version_config_inherits_voice_without_nested_version_bookkeeping(self):
        for base in [{"type": "custom", "voice": "Serena", "seed": "42"},
                     {"type": "clone", "ref_audio": "reference.wav", "ref_text": "Words"}]:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                script_path = os.path.join(tmp, "script.json")
                voice_path = os.path.join(tmp, "voices.json")
                Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]))
                current = {**base, "age_group": "adult", "versions": {"old": {"voice": "Ryan"}},
                           "version_timeline": [{"from_index": 1}], "style_timeline": [],
                           "candidates": [{"voice": "Ryan"}], "active_version": "old", "active_candidate": "c"}
                Path(voice_path).write_text(json.dumps({"Hero": current}))
                with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                     patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                    result = asyncio.run(voices_module.save_voice_version(
                        "Hero", voices_module.VoiceVersionRequest(version_id="teen", age_group="teen")))
                self.assertEqual({**base, "age_group": "teen"}, result["versions"]["teen"])
                saved = json.loads(Path(voice_path).read_text())["Hero"]
                self.assertEqual(result["versions"]["teen"], saved["versions"]["teen"])
                self.assertEqual(current["versions"]["old"], saved["versions"]["old"])
                self.assertEqual(current["active_version"], saved["active_version"])
                self.assertEqual(base["type"], saved["type"])

    def test_voice_versions_and_candidates_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}, {"speaker": "NARRATOR"}]), encoding="utf-8")
            Path(voice_path).write_text("{}", encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                asyncio.run(voices_module.save_voice_version(
                    "Hero", voices_module.VoiceVersionRequest(
                        version_id="teen", age_group="teen",
                        config={"type": "lora", "adapter_id": "hero-teen"})))
                asyncio.run(voices_module.add_voice_candidate(
                    "Hero", voices_module.VoiceCandidateRequest(
                        candidate_id="hero-alt", config={"type": "lora", "adapter_id": "hero-alt"})))
                selected = asyncio.run(voices_module.select_voice_version("Hero", "teen"))
                self.assertEqual("teen", selected["config"]["active_version"])
                self.assertEqual("teen", selected["config"]["age_group"])
                self.assertEqual("hero-teen", selected["config"]["adapter_id"])
                selected = asyncio.run(voices_module.select_voice_candidate("Hero", "hero-alt"))
                self.assertEqual("hero-alt", selected["config"]["active_candidate"])
                self.assertEqual("hero-alt", selected["config"]["adapter_id"])

    def test_narrator_strategy_requires_narrator_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "NARRATOR"}]), encoding="utf-8")
            Path(voice_path).write_text("{}", encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                result = asyncio.run(voices_module.save_narrator_strategy(
                    voices_module.NarratorStrategyRequest(strategy="chapter")))
                self.assertEqual("chapter", result["strategy"])
                self.assertEqual("chapter", json.loads(Path(voice_path).read_text())["NARRATOR"]["narrator_strategy"])

    def test_voice_version_rejects_unknown_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "voices.json")), \
                 self.assertRaises(voices_module.HTTPException) as caught:
                asyncio.run(voices_module.save_voice_version(
                    "Missing", voices_module.VoiceVersionRequest(version_id="v1")))
            self.assertEqual(404, caught.exception.status_code)

    def test_voice_candidate_can_be_deleted_and_clears_active_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"Hero": {"candidates": [
                {"candidate_id": "alt", "type": "lora"}], "active_candidate": "alt"}}), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                result = asyncio.run(voices_module.delete_voice_candidate("Hero", "alt"))
            self.assertEqual("deleted", result["status"])
            saved = json.loads(Path(voice_path).read_text(encoding="utf-8"))["Hero"]
            self.assertEqual([], saved["candidates"])
            self.assertNotIn("active_candidate", saved)

    def test_persona_generation_can_target_one_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            tasks = voices_module.BackgroundTasks()
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "voices.json")), \
                 patch.object(voices_module, "check_global_gpu_lock"), \
                 patch.object(core_module, "claim_gpu_task", wraps=core_module.claim_gpu_task), \
                 patch.object(voices_module, "project_manager", SimpleNamespace(engine=None)), \
                 patch.object(voices_module, "run_process"):
                result = asyncio.run(voices_module.generate_personas(
                    tasks, voices_module.GeneratePersonasRequest(speaker="Hero", age_group="teen")))
            self.assertEqual("started", result["status"])
            self.assertIn("--speaker", tasks.tasks[0].args[2].args[0])
            self.assertIn("Hero", tasks.tasks[0].args[2].args[0])
            self.assertIn("--age-group", tasks.tasks[0].args[2].args[0])
            self.assertIn("teen", tasks.tasks[0].args[2].args[0])

    def test_has_a_voice_means_a_persona_or_an_assigned_voice_not_a_bare_entry(self):
        """The Voices tab writes a default custom entry for every character on
        render, so "in voice_config" is not the test; /api/voices and
        --new-only share tts.voice_is_set."""
        import tts
        self.assertFalse(tts.voice_is_set(None))
        self.assertFalse(tts.voice_is_set({"type": "custom", "voice": "Aiden", "character_style": "", "seed": "-1"}))
        self.assertTrue(tts.voice_is_set({"type": "custom", "voice": "Aiden", "description": "warm baritone"}))
        self.assertTrue(tts.voice_is_set({"type": "custom", "ref_audio": "x.wav"}))
        self.assertTrue(tts.voice_is_set({"type": "lora", "adapter_id": "a"}))
        self.assertTrue(tts.voice_is_set({"type": "design", "description": ""}))
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}, {"speaker": "Bare"}, {"speaker": "New"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"Hero": {"type": "lora", "adapter_id": "a"},
                                                    "Bare": {"type": "custom", "voice": "Aiden", "seed": "-1"}}), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                rows = {r["name"]: r["persona_pending"] for r in asyncio.run(voices_module.get_voices())}
        self.assertEqual({"Hero": False, "Bare": True, "New": True}, rows)

    def test_persona_generation_can_be_limited_to_characters_without_a_voice(self):
        """#602: new_only reaches generate_personas.py as --new-only; the
        default regenerates everyone, as before."""
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            commands = []
            for new_only in (True, False):
                tasks = voices_module.BackgroundTasks()
                with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                     patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "voices.json")), \
                     patch.object(voices_module, "check_global_gpu_lock"), \
                     patch.object(core_module, "claim_gpu_task", wraps=core_module.claim_gpu_task), \
                     patch.object(voices_module, "project_manager", SimpleNamespace(engine=None)), \
                     patch.object(voices_module, "run_process"):
                    asyncio.run(voices_module.generate_personas(
                        tasks, voices_module.GeneratePersonasRequest(new_only=new_only)))
                commands.append(tasks.tasks[0].args[2].args[0])
                self.assertTrue(core_module.is_task_running("persona"))
                core_module.release_pending_task_claims()
                self.assertFalse(core_module.is_task_running("persona"))
            self.assertIn("--new-only", commands[0])
            self.assertNotIn("--new-only", commands[1])
            self.assertFalse(voices_module.GeneratePersonasRequest().new_only)

    def test_voice_candidate_favorite_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"Hero": {"candidates": [{"candidate_id": "alt"}]}}), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                result = asyncio.run(voices_module.favorite_voice_candidate(
                    "Hero", "alt", voices_module.VoiceCandidateFavoriteRequest(favorite=True)))
            self.assertTrue(result["favorite"])
            self.assertTrue(json.loads(Path(voice_path).read_text(encoding="utf-8"))["Hero"]["candidates"][0]["favorite"])

    def test_narrator_preview_reports_selected_version_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "NARRATOR"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"NARRATOR": {
                "voice": "Ryan", "versions": {"dramatic": {"type": "custom", "voice": "Dylan"}}}}), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                result = asyncio.run(voices_module.preview_narrator(
                    voices_module.NarratorPreviewRequest(strategy="chapter", narrator_version="dramatic")))
            self.assertEqual("Dylan", result["selected"]["voice"])
            self.assertEqual("dramatic", result["narrator_version"])
            self.assertEqual("Dylan", json.loads(Path(voice_path).read_text(encoding="utf-8"))["NARRATOR"]["versions"]["dramatic"]["voice"])

    def test_gender_marker_does_not_treat_digit_suffix_as_gender(self):
        self.assertEqual("unknown", voices_module._infer_lora_gender({"name": "voice_f1"}))
        self.assertEqual("unknown", voices_module._infer_lora_gender({"name": "voice_m1"}))
        self.assertEqual("female", voices_module._infer_lora_gender({"name": "voice_f"}))

    def test_pitch_is_not_used_as_a_gender_classifier(self):
        low = {"voice_features": {"mean_f0": 90}}
        high = {"voice_features": {"mean_f0": 260}}
        self.assertEqual("unknown", voices_module._infer_lora_gender(low))
        self.assertEqual("unknown", voices_module._infer_lora_gender(high))
        self.assertEqual(
            "female", voices_module._infer_lora_gender(
                {"description": "warm mezzo voice",
                 "voice_features": {"mean_f0": 90}}))

    def test_voice_library_mutations_hold_lock_across_load_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_path = os.path.join(tmp, "voice_library.json")
            Path(library_path).write_text(
                json.dumps({"shared": {}, "casts": {}}), encoding="utf-8")
            barrier = threading.Barrier(8)

            def add_cast(index):
                barrier.wait()
                voice_library_module._mutate_voice_library(
                    lambda lib: lib["casts"].update({f"cast-{index}": {"members": {}}}))

            with patch.object(core_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(voice_library_module, "VOICE_LIBRARY_PATH", library_path):
                threads = [threading.Thread(target=add_cast, args=(index,)) for index in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            saved = json.loads(Path(library_path).read_text(encoding="utf-8"))
            self.assertEqual({f"cast-{index}" for index in range(8)}, set(saved["casts"]))

    def test_ready_flag_round_trips_and_survives_a_cast_apply(self):
        item = voices_module.VoiceConfigItem(type="custom", voice="Ryan", ready=True)
        self.assertTrue(item.model_dump()["ready"])
        self.assertFalse(voices_module.VoiceConfigItem(type="custom").model_dump()["ready"])
        lib = {"shared": {}, "casts": {"c": {"members": {"ELENA": {"config": {"type": "custom", "voice": "Ryan", "ready": True}}}}}}
        current = {"ELENA": {"type": "custom", "voice": "Aiden", "ready": True},
                   "BOB": {"type": "custom", "voice": "Aiden"}}
        out, applied = voice_library_module._apply_cast_mapping(lib, "c", {"ELENA": "ELENA", "BOB": "ELENA"}, current)
        self.assertEqual(["ELENA", "BOB"], applied)
        self.assertTrue(out["ELENA"]["ready"])       # the book's own approval is kept
        self.assertNotIn("ready", out["BOB"])        # the library entry's flag never leaks

    def test_voice_config_rejects_empty_ensemble_before_save(self):
        for members in (None, [], ["  "]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                voices_module.VoiceConfigItem(type="ensemble", members=members)

        configured = voices_module.VoiceConfigItem(
            type="ensemble", members=["Petra", "Subaru"])
        self.assertEqual(["Petra", "Subaru"], configured.members)

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
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=[{
                     "adapter_id": "voice", "name": "Voice", "gender": "unknown",
                     "description": "neutral", "type": "lora",
                 }]), \
                 patch.object(voices_module, "_make_llm_client", return_value=(client, "model")):
                voices_module._suggest_voices_impl(voices_module.SuggestVoicesRequest(max_lines=12))
        for i in range(12):
            self.assertIn(f"distinct line {i}", captured["prompt"])

    def test_generic_cast_keys_are_book_scoped(self):
        self.assertEqual(core_module.get_cast_member_key("Man", "book-01"), "man::book-01")
        self.assertEqual(core_module.get_cast_member_key("Man", "book-05"), "man::book-05")
        self.assertEqual(core_module.get_cast_member_key("Holo", "book-01"), "holo")
        with self.assertRaises(ValueError):
            core_module.get_cast_member_key("Guard", None)

    def test_cast_usage_counts_distinct_members_not_books(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "holo": {"name": "Holo", "config": {"adapter_id": "v1"},
                     "assignments": {"b1": {"line_count": 10}, "b2": {"line_count": 20}}},
            "man::b1": {"name": "Man", "config": {"adapter_id": "v1"},
                        "assignments": {"b1": {"line_count": 3}}},
        }}}}
        usage = core_module.get_cast_adapter_usage(lib, "series")
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
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(voices_module, "get_active_book_id", return_value="book-01"), \
                 patch.object(voices_module, "_load_voice_library", return_value={"shared": {}, "casts": {"series": {"members": {}}}}), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(voices_module, "_make_llm_client", return_value=(client, "model")):
                result = voices_module._suggest_voices_impl(
                    voices_module.SuggestVoicesRequest(cast="series", max_lines=4))
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
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(voices_module, "get_active_book_id", return_value="b1"), \
                 patch.object(voices_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(voices_module, "_make_llm_client", return_value=(client, "model")), \
                 patch.object(voices_module, "get_current_status", return_value={"context_length": None}):
                result = voices_module._suggest_voices_impl(voices_module.SuggestVoicesRequest(max_lines=4))
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
        self.assertEqual(voices_module._infer_lora_age({"id": "warm_baritone_40s_m"}), "middle_aged")
        self.assertEqual(voices_module._infer_lora_age({"description": "elderly gravelly bass"}), "elderly")
        self.assertEqual(voices_module._infer_lora_age({"age": "40s"}), "middle_aged")
        self.assertEqual(voices_module._infer_lora_age({"age": 67}), "elderly")

    def test_numeric_age_boundaries_and_precedence(self):
        expected = {"aged 12": "child", "13 years old": "teen", "19-year-old": "teen",
                    "20 years old": "young_adult", "aged 39": "adult",
                    "40-year-old": "middle_aged", "aged 60": "elderly"}
        for text, group in expected.items():
            self.assertEqual(voices_module._infer_age_group(text), group, text)
        self.assertEqual(voices_module._infer_age_group("young man, aged 50"), "middle_aged")

    def test_age_parser_handles_invalid_ambiguous_and_decade_values(self):
        expected = {
            "0": "unknown", "1": "child", "67": "elderly", "120": "elderly",
            "121": "unknown", "aged 12 then aged 60": "child", "under 12": "child",
            "20s": "young_adult", "30s": "adult", "40s": "middle_aged",
            "50s": "middle_aged", "60s": "elderly", "70s": "elderly", "80s": "elderly",
            "90s": "elderly",
        }
        for text, group in expected.items():
            self.assertEqual(voices_module._infer_age_group(text), group, text)

    def test_llm_traits_replace_local_traits_only_with_stronger_authority(self):
        candidates = [
            {"adapter_id": "female", "name": "F", "type": "lora", "gender": "female",
             "age_group": "adult", "description": ""},
            {"adapter_id": "male", "name": "M", "type": "lora", "gender": "male",
             "age_group": "adult", "description": ""},
        ]

        def suggest(confidence):
            parsed = {"characters": [{
                "name": "HERO", "ranked_adapter_ids": ["male", "female"],
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
                # The speaker LABEL carries the trait, not the dialogue. This
                # test used to give "Hero" the line "She was an old woman who
                # entered quietly" and assert Hero was therefore female and
                # elderly - encoding the inverted inference as correct. A
                # character's words describe whoever they are talking about, so
                # dialogue was removed as a trait source; the label is a
                # legitimate one and keeps this test's actual subject, which is
                # the authority ordering between local and LLM traits.
                Path(script_path).write_text(json.dumps([
                    {"speaker": "HERO",
                     "text": "She was an old woman who entered quietly."}
                ]), encoding="utf-8")
                with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                     patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                     patch.object(voices_module, "get_active_book_id", return_value="b1"), \
                     patch.object(voices_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                     patch.object(voices_module, "_build_lora_candidates", return_value=candidates), \
                     patch.object(voices_module, "_make_llm_client", return_value=(client, "model")), \
                     patch.object(voices_module, "get_current_status", return_value={"context_length": None}):
                    return voices_module._suggest_voices_impl(
                        voices_module.SuggestVoicesRequest(max_lines=4)
                    )["suggestions"]["HERO"]

        # "HERO" carries no gender and the dialogue is no longer a trait
        # source, so local traits are unknown. A non-authoritative LLM claim
        # must not be adopted on top of that - "unknown" is a real answer, and
        # every consumer treats it as do-not-filter, do-not-penalise.
        for confidence in ("unknown", "low"):
            suggestion = suggest(confidence)
            self.assertEqual(suggestion["character_gender"], "unknown", confidence)
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
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", os.path.join(tmp, "missing.json")), \
                 patch.object(voices_module, "get_active_book_id", return_value="b1"), \
                 patch.object(voices_module, "_load_voice_library", return_value={"shared": {}, "casts": {}}), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=candidates), \
                 patch.object(voices_module, "_make_llm_client", return_value=(client, "model")), \
                 patch.object(voices_module, "get_current_status", return_value={"context_length": None}):
                suggestion = voices_module._suggest_voices_impl(
                    voices_module.SuggestVoicesRequest(max_lines=4))["suggestions"]["King"]
        self.assertEqual(suggestion["character_gender"], "male")
        self.assertEqual(suggestion["character_age_group"], "elderly")
        self.assertIn("LM accepted age=elderly", suggestion["trait_evidence"])
        self.assertNotIn("elderly woman", suggestion["trait_evidence"])
        self.assertEqual(suggestion["llm_trait_evidence"], "An elderly woman speaks")

    def test_favorite_wins_from_lm_rank_two_but_not_over_a_hard_mismatch(self):
        first = {"adapter_id": "first", "gender": "male", "age_group": "adult", "description": ""}
        starred = {"adapter_id": "starred", "gender": "male", "age_group": "adult", "description": "", "favorite": True}
        traits = {"gender": "male", "gender_confidence": "high",
                  "age_group": "adult", "age_confidence": "high"}
        chosen, ranked, *_ = voices_module.get_voice_allocation(
            "", [first, starred], ["first", "starred"], traits, None, {}, "minor")
        self.assertEqual(chosen, "starred")
        self.assertEqual(ranked[0], "starred")
        # a starred voice of the wrong age (authoritative) does not jump the queue
        old_star = dict(starred, age_group="elderly")
        chosen, *_ = voices_module.get_voice_allocation(
            "", [first, old_star], ["first", "starred"], traits, None, {}, "minor")
        self.assertEqual(chosen, "first")
        # the heuristic ranker also puts favorites first within an age tier
        order = voices_module._rank_heuristic_candidates("", [first, starred], "male", "adult")
        self.assertEqual(order[0], "starred")

    def test_favorite_toggle_round_trips_through_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "voice_library.json")
            with patch.object(voice_library_module, "VOICE_LIBRARY_PATH", path), \
                 patch.object(core_module, "VOICE_LIBRARY_PATH", path):
                import asyncio
                first = asyncio.run(voice_library_module.voice_library_toggle_favorite("alto"))
                self.assertEqual(first, {"favorite": True, "favorites": ["alto"]})
                second = asyncio.run(voice_library_module.voice_library_toggle_favorite("alto"))
                self.assertEqual(second, {"favorite": False, "favorites": []})

    def test_low_confidence_traits_do_not_hard_filter(self):
        candidates = [
            {"adapter_id": "male", "gender": "male", "age_group": "adult", "description": ""},
            {"adapter_id": "female", "gender": "female", "age_group": "elderly", "description": ""},
        ]
        traits = {"gender": "female", "gender_confidence": "low",
                  "age_group": "elderly", "age_confidence": "low"}
        chosen, _ranked, _new, fallback, _mismatch = voices_module.get_voice_allocation(
            "", candidates, ["male", "female"], traits, None, {}, "minor")
        self.assertEqual(chosen, "male")
        self.assertFalse(fallback)

    def test_gender_fallback_and_recurring_mismatch(self):
        male = {"adapter_id": "male", "gender": "male", "age_group": "adult", "description": ""}
        traits = {"gender": "female", "gender_confidence": "high",
                  "age_group": "young_adult", "age_confidence": "high"}
        chosen, _ranked, _new, fallback, mismatch = voices_module.get_voice_allocation(
            "", [male], ["male"], traits, None, {}, "major")
        self.assertEqual(chosen, "male")
        self.assertTrue(fallback)
        self.assertFalse(mismatch)
        chosen, _ranked, is_new, _fallback, mismatch = voices_module.get_voice_allocation(
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
            chosen, _ranked, _new, fallback, _mismatch = voices_module.get_voice_allocation(
                "", candidates, [c["adapter_id"] for c in candidates],
                traits, None, {}, "major")
            self.assertEqual(chosen, expected)
            self.assertEqual(fallback, expected_fallback)

    def test_recurring_mismatch_requires_authoritative_trait_confidence(self):
        male = {"adapter_id": "male", "gender": "male", "age_group": "child", "description": ""}
        for confidence in ("unknown", "low", "medium", "high"):
            traits = {"gender": "female", "gender_confidence": confidence,
                      "age_group": "elderly", "age_confidence": confidence}
            _chosen, _ranked, _new, _fallback, mismatch = voices_module.get_voice_allocation(
                "", [male], ["male"], traits, "male", {}, "major")
            self.assertEqual(mismatch, confidence in ("medium", "high"), confidence)

    def test_character_trait_evidence_prefers_label_over_dialogue(self):
        traits = voices_module._infer_character_traits(
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
            with patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path), \
                 patch.object(voices_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(core_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(voices_module, "get_active_book_id", return_value="book-05"), \
                 patch.object(voices_module, "_script_line_counts", return_value={"Man": 4}), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=[candidate]):
                result = voices_module._apply_voice_suggestions({"Man": suggestion}, "series")
            voice = json.loads(Path(voice_path).read_text(encoding="utf-8"))
            library = json.loads(Path(library_path).read_text(encoding="utf-8"))
        self.assertEqual(voice["Man"]["character_style"], "Brief wary delivery")
        self.assertEqual("v1", voice["Man"]["persona_voice_audit"]["voice_adapter_id"])
        self.assertEqual("small suspicious role", voice["Man"]["persona_voice_audit"]["suggestion_reason"])
        for field in core_module.get_trait_assignment_metadata(suggestion):
            self.assertEqual(voice["Man"][field], suggestion[field], field)
        member = library["casts"]["series"]["members"]["man::book-05"]
        self.assertEqual(member["config"]["adapter_id"], "v1")
        for field in core_module.get_trait_assignment_metadata(suggestion):
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
        config, applied = voice_library_module._apply_cast_mapping(
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
        self.assertNotIn("man", voice_library_module._cast_match_pool(lib, "series", "book-05"))

    def test_numbered_generic_cast_keys_are_book_scoped(self):
        self.assertEqual(core_module.get_cast_member_key("Man 1", "b5"), "man 1::b5")
        self.assertEqual(core_module.get_cast_member_key("Guard #2", "b5"), "guard #2::b5")

    def test_shared_narrator_is_reused_and_counted(self):
        lib = {"shared": {"narrator": {"name": "Narrator", "config": {"adapter_id": "v1"},
                                         "assignments": {"b1": {"line_count": 100}}}},
               "casts": {"series": {"members": {}}}}
        self.assertEqual(core_module.get_cast_adapter_usage(lib, "series")["v1"]["character_count"], 1)
        self.assertIs(core_module.get_cast_storage_pool(lib, "series", "Narrator"), lib["shared"])

    def test_bulk_generic_mapping_resolves_per_book_member(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "man::b1": {"name": "Man", "config": {"adapter_id": "v1"}, "generic": True,
                         "book_id": "b1", "assignments": {"b1": {"character_style": "one"}}},
            "man::b5": {"name": "Man", "config": {"adapter_id": "v2"}, "generic": True,
                         "book_id": "b5", "assignments": {"b5": {"character_style": "five"}}},
        }}}}
        config, _ = voice_library_module._apply_cast_mapping(
            lib, "series", {"Man": "man::b1"}, {}, chars={"Man": 2}, book_id="b5")
        self.assertEqual(config["Man"]["adapter_id"], "v2")
        self.assertEqual(config["Man"]["character_style"], "five")

    def test_cast_member_matches_by_remembered_label_and_alias(self):
        lib = {"shared": {}, "casts": {"series": {"members": {
            "natsuki subaru": {"name": "NATSUKI SUBARU", "known_as": ["NATSUKI SUBARU", "Subaru"],
                               "config": {"type": "lora", "adapter_id": "v1"}},
            "rem": {"name": "REM", "config": {"type": "lora", "adapter_id": "v2"}},
        }}}}
        aliases = {"BARUSU": "NATSUKI SUBARU"}
        pool = voice_library_module._cast_match_pool(lib, "series", "b2", aliases=aliases)
        self.assertEqual(pool["natsuki subaru"]["labels"], ["NATSUKI SUBARU", "Subaru", "BARUSU"])
        # A legacy member without known_as answers to its name only.
        self.assertEqual(pool["rem"]["labels"], ["REM"])
        by_char = {p["character"]: p["match"] for p in voice_library_module._build_match_proposals(
            {"SUBARU": 50, "Barusu": 3, "Remu": 2, "EMILIA": 9}, pool)}
        self.assertEqual((by_char["SUBARU"]["key"], by_char["SUBARU"]["score"], by_char["SUBARU"]["via"]),
                         ("natsuki subaru", 1.0, "known_as"))
        self.assertTrue(by_char["SUBARU"]["exact"])
        self.assertEqual((by_char["Barusu"]["key"], by_char["Barusu"]["via"]), ("natsuki subaru", "alias"))
        self.assertEqual((by_char["Remu"]["key"], by_char["Remu"]["via"]), ("rem", "name"))
        self.assertFalse(by_char["Remu"]["exact"])
        self.assertIsNone(by_char["EMILIA"])

    def test_applying_a_cast_remembers_the_new_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_path = os.path.join(tmp, "voice_library.json")
            with open(library_path, "w", encoding="utf-8") as f:
                json.dump({"shared": {"narrator": {"name": "Narrator", "config": {}}},
                           "casts": {"series": {"members": {
                               "subaru": {"name": "Subaru", "config": {"adapter_id": "v1"}},
                               "man 1::b1": {"name": "Man 1", "generic": True, "book_id": "b1",
                                             "config": {"adapter_id": "v3"}},
                           }}}}, f)
            mapping = {"NATSUKI SUBARU": "subaru", "Man 2": "man 1::b1", "Chronicler": "narrator",
                       "Ghost": "missing-key"}
            with patch.object(voice_library_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(core_module, "VOICE_LIBRARY_PATH", library_path):
                for _ in range(2):  # idempotent
                    voice_library_module._remember_applied_labels(
                        "series", mapping, ["NATSUKI SUBARU", "Man 2", "Chronicler", "Ghost"])
            with open(library_path, encoding="utf-8") as f:
                lib = json.load(f)
            self.assertEqual(lib["casts"]["series"]["members"]["subaru"]["known_as"],
                             ["Subaru", "NATSUKI SUBARU"])
            # Generic labels are not identities and are never remembered.
            self.assertNotIn("known_as", lib["casts"]["series"]["members"]["man 1::b1"])
            self.assertEqual(lib["shared"]["narrator"]["known_as"], ["Narrator", "Chronicler"])
            # The next book's match sees the remembered label as exact.
            pool = voice_library_module._cast_match_pool(lib, "series", "b3", aliases={})
            match = voice_library_module._build_match_proposals({"Natsuki Subaru": 1}, pool)[0]["match"]
            self.assertEqual((match["key"], match["exact"], match["via"]), ("subaru", True, "known_as"))

    def test_library_entry_records_known_as_and_tolerates_legacy_entries(self):
        entry = core_module._make_library_entry("Holo", {"type": "lora"}, 3, "b1")
        self.assertEqual(entry["known_as"], ["Holo"])
        resaved = core_module._make_library_entry("HOLO", {"type": "lora"}, 5, "b2", existing=entry)
        self.assertEqual(resaved["known_as"], ["Holo"])  # same identity, different casing
        self.assertEqual(core_module.get_member_labels({"name": "Holo"}), ["Holo"])
        self.assertEqual(core_module.add_known_label(["Holo"], "Man 1"), ["Holo"])

    def test_stale_suggestion_is_rejected(self):
        with patch.object(voices_module, "_build_lora_candidates", return_value=[]), \
             patch.object(voices_module, "_script_line_counts", return_value={"Narrator": 2}), \
             patch.object(voices_module, "get_active_book_id", return_value="book-b"):
            with self.assertRaisesRegex(Exception, "different book"):
                voices_module._apply_voice_suggestions(
                    {"Narrator": {"adapter_id": "v1", "book_id": "book-a"}}, None)

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
            with patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path), \
                 patch.object(voices_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(core_module, "VOICE_LIBRARY_PATH", library_path), \
                 patch.object(voices_module, "get_active_book_id", return_value="b1"), \
                 patch.object(voices_module, "_script_line_counts", return_value={"Narrator": 100}), \
                 patch.object(voices_module, "_build_lora_candidates", return_value=[candidate]):
                voices_module._apply_voice_suggestions({"Narrator": suggestion}, "series")
            library = json.loads(Path(library_path).read_text(encoding="utf-8"))
        self.assertEqual(library["shared"]["narrator"]["config"]["adapter_id"], "v1")
        self.assertNotIn("narrator", library["casts"]["series"]["members"])

    def test_persona_to_voice_audit_can_be_updated_without_clobbering_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "annotated_script.json")
            voice_path = os.path.join(tmp, "voice_config.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Man"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"Man": {
                "persona_voice_audit": {"voice_adapter_id": "old", "persona_ref": "p1"}}}), encoding="utf-8")
            request = voices_module.PersonaVoiceAuditRequest(suggestion_reason="corrected")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                    patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                result = asyncio.run(voices_module.update_persona_voice_audit("Man", request))
            self.assertEqual("corrected", result["persona_voice_audit"]["suggestion_reason"])
            self.assertEqual("old", result["persona_voice_audit"]["voice_adapter_id"])
            self.assertEqual("p1", result["persona_voice_audit"]["persona_ref"])

    def test_prompt_construction_errors_return_marked_copy_without_calling_model(self):
        from unittest.mock import Mock
        fake_llama = SimpleNamespace(Llama=object, llama_supports_gpu_offload=lambda: True)
        path = Path(__file__).resolve().parent.parent.parent / "llm_enricher.py"
        with patch.dict(sys.modules, {"llama_cpp": fake_llama}):
            spec = importlib.util.spec_from_file_location("test_llm_enricher_prompt_errors", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        enricher = module.LLMEnricher.__new__(module.LLMEnricher)
        enricher.fields = ["emotional_tone"]
        enricher.llm = Mock(return_value={"choices": [{"text": '{"emotional_tone":"calm"}'}]})
        for field in ("start", "end"):
            for value in (None, "bad timestamp", [], {}):
                with self.subTest(field=field, value=value):
                    chunk = {"text": "Keep this original.", "speaker": "Alice",
                             "start": 0.0, "end": 1.0, "user_metadata": {"keep": True}}
                    chunk[field] = value
                    original = json.loads(json.dumps(chunk))
                    with self.assertLogs(module.logger, level="ERROR") as logs:
                        result = enricher.enrich_transcript_chunk(chunk)
                    self.assertEqual({**original, "_enrichment_failed": True}, result)
                    self.assertIsNot(chunk, result)
                    self.assertEqual(original, chunk)
                    self.assertTrue(any("Error during LLM enrichment" in line for line in logs.output))
                    enricher.llm.assert_not_called()
        chunk = {"text": "A valid line.", "speaker": "Alice", "start": 0.0, "end": 1.0}
        result = enricher.enrich_transcript_chunk(chunk)
        self.assertEqual({**chunk, "emotional_tone": "calm"}, result)
        enricher.llm.assert_called_once()
        self.assertIn("Start Time: 0.00s", enricher.llm.call_args.args[0])
        self.assertEqual({"max_tokens": 150, "stop": ["</s>"], "temperature": 0.7}, enricher.llm.call_args.kwargs)
        self.assertNotIn("emotional_tone", chunk)

    def test_selective_enrichment_prompt(self):
        fake_llama = SimpleNamespace(Llama=object, llama_supports_gpu_offload=lambda: True)
        path = Path(__file__).resolve().parent.parent.parent / "llm_enricher.py"
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

    def test_enrichment_write_failure_preserves_previous_output(self):
        fake_llama = SimpleNamespace(Llama=object, llama_supports_gpu_offload=lambda: True)
        path = Path(__file__).resolve().parent.parent.parent / "llm_enricher.py"
        with patch.dict(sys.modules, {"llama_cpp": fake_llama}):
            spec = importlib.util.spec_from_file_location("test_llm_enricher_atomic", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "enriched.json")
            output.write_text('[{"old": true}]')
            with patch.object(module.os, "replace", side_effect=OSError("rename failed")):
                with self.assertRaisesRegex(OSError, "rename failed"):
                    module.save_enriched_transcript([{"new": True}], str(output))
            self.assertEqual('[{"old": true}]', output.read_text())
            self.assertEqual(["enriched.json"], sorted(p.name for p in Path(tmp).iterdir()))


class VoiceMetadataIdentityTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.script = Path(directory.name)/'script.json'
        self.config = Path(directory.name)/'voices.json'
        self.script.write_text(json.dumps([{'speaker': 'Hero', 'text': 'Hello.'}]), encoding='utf-8')
        self.config.write_text(json.dumps({'Hero': {'type': 'custom', 'voice': 'Ryan',
            'candidates': [{'candidate_id': 'other', 'type': 'custom', 'voice': 'Serena'}]},
            'Unrelated': {'voice': 'Dylan'}}), encoding='utf-8')
        self.script_bytes = self.script.read_bytes()
        for field, path in (('SCRIPT_PATH', self.script), ('VOICE_CONFIG_PATH', self.config)):
            patcher = patch.object(voices_module, field, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        api = FastAPI()
        api.include_router(voices_module.router)
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def test_explicit_version_age_wins_nested_config_on_save_and_select(self):
        for index, nested_age in enumerate((None, 7, [], {}, '', 'elderly')):
            with self.subTest(nested_age=nested_age):
                version = 'v' + str(index)
                payload = {'version_id': version, 'age_group': 'teen',
                           'config': {'type': 'custom', 'voice': 'Serena', 'age_group': nested_age}}
                before = json.dumps(payload)
                response = self.client.post('/api/voices/Hero/versions', json=payload)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual('teen', response.json()['versions'][version]['age_group'])
                selected = self.client.post('/api/voices/Hero/versions/'+version+'/select')
                self.assertEqual(200, selected.status_code, selected.text)
                self.assertEqual('teen', selected.json()['config']['age_group'])
                stored = json.loads(self.config.read_text())
                self.assertEqual('teen', stored['Hero']['versions'][version]['age_group'])
                self.assertEqual('Serena', stored['Hero']['voice'])
                self.assertEqual({'voice': 'Dylan'}, stored['Unrelated'])
                self.assertEqual(before, json.dumps(payload))
        self.assertEqual(self.script_bytes, self.script.read_bytes())
        before = self.config.read_bytes()
        rejected = self.client.post('/api/voices/Hero/versions',
                                    json={'version_id': 'bad', 'age_group': None, 'config': {}})
        self.assertEqual(422, rejected.status_code)
        self.assertEqual(before, self.config.read_bytes())

    def test_explicit_candidate_id_supports_replace_select_favorite_and_delete(self):
        for index, nested_id in enumerate(('other', None, [], {}, 7)):
            with self.subTest(nested_id=nested_id):
                payload = {'candidate_id': 'wanted', 'config': {
                    'candidate_id': nested_id, 'type': 'custom', 'voice': 'Dylan',
                    'character_style': 'variant '+str(index)}}
                before = json.dumps(payload)
                response = self.client.post('/api/voices/Hero/candidates', json=payload)
                self.assertEqual(200, response.status_code, response.text)
                candidates = response.json()['candidates']
                self.assertEqual(1, len([c for c in candidates if c['candidate_id']=='wanted']))
                self.assertEqual({'candidate_id': 'other', 'type': 'custom', 'voice': 'Serena'},
                                 next(c for c in candidates if c['candidate_id']=='other'))
                selected = self.client.post('/api/voices/Hero/candidates/wanted/select')
                self.assertEqual(200, selected.status_code, selected.text)
                self.assertEqual('wanted', selected.json()['config']['active_candidate'])
                self.assertEqual('variant '+str(index), selected.json()['config']['character_style'])
                favorite = self.client.post('/api/voices/Hero/candidates/wanted/favorite', json={'favorite': True})
                self.assertEqual(200, favorite.status_code, favorite.text)
                self.assertTrue(favorite.json()['favorite'])
                stored = json.loads(self.config.read_text())
                self.assertTrue(next(c for c in stored['Hero']['candidates'] if c['candidate_id']=='wanted')['favorite'])
                self.assertEqual(before, json.dumps(payload))
        deleted = self.client.delete('/api/voices/Hero/candidates/wanted')
        self.assertEqual(200, deleted.status_code, deleted.text)
        stored = json.loads(self.config.read_text())
        self.assertEqual(['other'], [c['candidate_id'] for c in stored['Hero']['candidates']])
        self.assertNotIn('active_candidate', stored['Hero'])
        self.assertEqual({'voice': 'Dylan'}, stored['Unrelated'])
        self.assertEqual(self.script_bytes, self.script.read_bytes())


class VoiceListShapeTests(unittest.TestCase):
    def test_wrong_top_level_script_shapes_return_empty_list_without_writes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(voices_module.router)
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            script = Path(tmp) / 'script.json'
            config = Path(tmp) / 'voices.json'
            config.write_text('{"Hero":{"type":"lora","adapter_id":"a"}}')
            original_config = config.read_bytes()
            with patch.object(voices_module, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices_module, 'VOICE_CONFIG_PATH', str(config)):
                for value in ({'speaker': 'Hero'}, 'Hero', 7, True, None):
                    with self.subTest(value=value):
                        script.write_text(json.dumps(value))
                        original_script = script.read_bytes()
                        response = client.get('/api/voices')
                        self.assertEqual(200, response.status_code)
                        self.assertEqual([], response.json())
                        self.assertEqual(original_script, script.read_bytes())
                        self.assertEqual(original_config, config.read_bytes())

    def test_non_object_rows_do_not_hide_valid_speakers_or_change_source(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(voices_module.router)
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            script = Path(tmp) / 'script.json'
            config = Path(tmp) / 'voices.json'
            script.write_text(json.dumps([None, 'bad', 7, True, [],
                {'speaker': 'Hero'}, {'type': 'Narrator'}, {'speaker': 'Hero'}]))
            original_script = script.read_bytes()
            voice = {'type': 'lora', 'adapter_id': 'a', 'extra': {'preserve': 1}}
            config.write_text(json.dumps({'Hero': voice}))
            original_config = config.read_bytes()
            with patch.object(voices_module, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices_module, 'VOICE_CONFIG_PATH', str(config)):
                response = client.get('/api/voices')
            self.assertEqual(200, response.status_code)
            self.assertEqual([{'name': 'Hero', 'config': voice, 'persona_pending': False},
                {'name': 'Narrator', 'config': {}, 'persona_pending': True}], response.json())
            self.assertEqual(original_script, script.read_bytes())
            self.assertEqual(original_config, config.read_bytes())

    def test_wrong_config_shapes_use_empty_config_without_overwriting_file(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(voices_module.router)
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            script = Path(tmp) / 'script.json'
            config = Path(tmp) / 'voices.json'
            script.write_text('[{"speaker":"Hero"}]')
            original_script = script.read_bytes()
            with patch.object(voices_module, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices_module, 'VOICE_CONFIG_PATH', str(config)):
                for value in ([], ['bad'], 'bad', 7, True, None):
                    with self.subTest(value=value):
                        config.write_text(json.dumps(value))
                        original_config = config.read_bytes()
                        response = client.get('/api/voices')
                        self.assertEqual(200, response.status_code)
                        self.assertEqual([{'name': 'Hero', 'config': {},
                            'persona_pending': True}], response.json())
                        self.assertEqual(original_script, script.read_bytes())
                        self.assertEqual(original_config, config.read_bytes())


class LibraryConfigShapeTests(unittest.TestCase):
    def test_unusable_config_is_not_saved_to_cast_or_changed(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(voice_library_module.router)
        library = {'favorites': [], 'shared': {}, 'casts': {'series': {'members': {'existing': {
            'name': 'Existing', 'config': {'type': 'custom', 'voice': 'Ryan'}}}}}}
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            config = Path(tmp) / 'voices.json'
            library_path = Path(tmp) / 'library.json'
            library_path.write_text(json.dumps(library))
            with patch.object(voice_library_module, 'VOICE_CONFIG_PATH', str(config)), \
                 patch.object(voice_library_module, 'SCRIPT_PATH', str(Path(tmp) / 'script.json')), \
                 patch.object(voice_library_module, 'VOICE_LIBRARY_PATH', str(library_path)), \
                 patch.object(core_module, 'VOICE_LIBRARY_PATH', str(library_path)), \
                 patch.object(voice_library_module, '_script_line_counts', return_value={'Hero': 1}), \
                 patch.object(voice_library_module, 'get_active_book_id', return_value='book'):
                for value in ([], ['bad'], 'bad', 7, True, None):
                    with self.subTest(value=value):
                        config.write_text(json.dumps(value))
                        original = config.read_bytes()
                        response = client.post('/api/voice_library/save', json={
                            'cast': 'series', 'characters': ['Hero']})
                        self.assertEqual(200, response.status_code)
                        self.assertEqual({'cast': [], 'shared': []}, response.json()['saved'])
                        self.assertEqual(original, config.read_bytes())
                        self.assertEqual(library, json.loads(library_path.read_bytes()))
                voice = {'type': 'custom', 'voice': 'Ryan', 'description': 'steady'}
                config.write_text(json.dumps({'Hero': voice}))
                original = config.read_bytes()
                response = client.post('/api/voice_library/save', json={
                    'cast': 'series', 'characters': ['Hero']})
                self.assertEqual(200, response.status_code)
                self.assertEqual({'cast': ['Hero'], 'shared': []}, response.json()['saved'])
                saved = json.loads(library_path.read_bytes())
                self.assertEqual(voice, saved['casts']['series']['members']['hero']['config'])
                self.assertEqual(library['casts']['series']['members']['existing'],
                    saved['casts']['series']['members']['existing'])
                self.assertEqual(original, config.read_bytes())


class SuggestionSelectionMetadataTests(unittest.TestCase):
    def test_new_adapter_clears_active_markers_and_preserves_history(self):
        candidate = {'adapter_id': 'new', 'type': 'lora', 'name': 'New'}
        suggestion = {'adapter_id': 'new', 'character_style': 'calm', 'book_id': 'book'}
        for cast in (None, 'series'):
            with self.subTest(cast=cast), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / 'voices.json'
                library_path = Path(tmp) / 'library.json'
                history = {'old-version': {'type': 'lora', 'adapter_id': 'old'}}
                candidates = [{'candidate_id': 'old-candidate', 'config': {'adapter_id': 'old'}}]
                old_voice = {'type': 'lora', 'adapter_id': 'old',
                    'active_candidate': 'old-candidate', 'active_version': 'old-version',
                    'versions': history, 'candidates': candidates, 'description': 'steady'}
                other = {'type': 'custom', 'voice': 'Ryan'}
                config.write_text(json.dumps({'Hero': old_voice, 'Other': other}))
                library_path.write_text(json.dumps({'shared': {}, 'casts': {'series': {'members': {}}}}))
                with patch.object(voices_module, 'VOICE_CONFIG_PATH', str(config)), \
                     patch.object(voices_module, 'VOICE_LIBRARY_PATH', str(library_path)), \
                     patch.object(core_module, 'VOICE_LIBRARY_PATH', str(library_path)), \
                     patch.object(voices_module, '_script_line_counts', return_value={'Hero': 1}), \
                     patch.object(voices_module, 'get_active_book_id', return_value='book'), \
                     patch.object(voices_module, '_build_lora_candidates', return_value=[candidate]):
                    result = voices_module._apply_voice_suggestions({'Hero': suggestion}, cast)
                self.assertEqual(['Hero'], result['applied'])
                saved = json.loads(config.read_bytes())
                voice = saved['Hero']
                self.assertEqual('new', voice['adapter_id'])
                self.assertEqual('lora_models/new', voice['adapter_path'])
                self.assertEqual('calm', voice['character_style'])
                self.assertNotIn('active_candidate', voice)
                self.assertNotIn('active_version', voice)
                self.assertEqual(history, voice['versions'])
                self.assertEqual(candidates, voice['candidates'])
                self.assertEqual('steady', voice['description'])
                self.assertEqual(other, saved['Other'])
                if cast:
                    member = json.loads(library_path.read_bytes())['casts']['series']['members']['hero']
                    self.assertEqual('new', member['config']['adapter_id'])
                    self.assertNotIn('active_candidate', member['config'])
                    self.assertNotIn('active_version', member['config'])


class DialogueCollectionWorkTests(unittest.TestCase):
    def test_per_character_uniqueness_is_linear_and_keeps_first_seen_order(self):
        class StopBeforeInference(Exception):
            pass

        class CountedText(str):
            comparisons = 0
            __hash__ = str.__hash__

            def strip(self):
                return self

            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)

        for count in (128, 512):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                lines = [CountedText('line-%04d' % index) for index in range(count)]
                repeated = [CountedText(str(line)) for line in lines]
                rows = [{'speaker': 'Hero', 'text': line} for line in lines + repeated]
                rows += [{'speaker': 'Other', 'text': CountedText('line-0000')}]
                script = Path(tmp) / 'script.json'
                script.write_text('[]')
                before = script.read_bytes()
                collected = {}

                def capture(speaker, _profile, dialogue):
                    collected[speaker] = [str(line) for line in dialogue]
                    if speaker == 'Other':
                        raise StopBeforeInference()
                    return {'gender': 'unknown', 'age_group': 'unknown',
                        'gender_confidence': 'unknown', 'age_confidence': 'unknown'}

                CountedText.comparisons = 0
                with patch.object(voices_module, 'SCRIPT_PATH', str(script)), \
                     patch.object(voices_module, 'VOICE_CONFIG_PATH', str(Path(tmp) / 'missing.json')), \
                     patch.object(voices_module.json, 'load', return_value=rows), \
                     patch.object(voices_module, '_build_lora_candidates', return_value=[{'adapter_id': 'a'}]), \
                     patch.object(voices_module, '_load_voice_library', return_value={'shared': {}, 'casts': {}}), \
                     patch.object(voices_module, '_script_line_counts', return_value={}), \
                     patch.object(voices_module, 'get_active_book_id', return_value='book'), \
                     patch.object(voices_module, '_infer_character_traits', side_effect=capture), \
                     patch.object(voices_module, '_make_llm_client') as client:
                    with self.assertRaises(StopBeforeInference):
                        voices_module._suggest_voices_impl(voices_module.SuggestVoicesRequest())
                self.assertEqual([str(line) for line in lines], collected['Hero'])
                self.assertEqual(['line-0000'], collected['Other'])
                self.assertLessEqual(CountedText.comparisons, 3 * count)
                self.assertEqual(before, script.read_bytes())
                client.assert_not_called()


class MissingBulkBookTests(unittest.TestCase):
    def test_missing_selected_books_report_errors_without_blocking_valid_books(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(voice_library_module.router)
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            root = Path(tmp)
            scripts = root / 'scripts'
            scripts.mkdir()
            (scripts / 'directory.json').mkdir()
            (scripts / 'missing.voice_config.json').write_text('{"Orphan":{"preserve":true}}')
            (scripts / 'directory.voice_config.json').write_text('{"Other":{"preserve":true}}')
            (scripts / 'valid.json').write_text('[{"speaker":"Hero","text":"A line."}]')
            (scripts / 'empty.json').write_text('[{"speaker":"Other","text":"Other line."}]')
            (scripts / 'valid.voice_config.json').write_text('{"Other":{"type":"custom","voice":"Ryan"}}')
            voice = {'type': 'lora', 'adapter_id': 'new', 'adapter_path': 'lora_models/new'}
            library = root / 'library.json'
            library.write_text(json.dumps({'favorites': [], 'shared': {}, 'casts': {'series': {
                'members': {'hero': {'name': 'Hero', 'config': voice}}}}}))
            preserved = {p.name: p.read_bytes() for p in scripts.iterdir()
                         if p.is_file() and p.name != 'valid.voice_config.json'}
            with patch.object(voice_library_module, 'SCRIPTS_DIR', str(scripts)), \
                 patch.object(core_module, 'SCRIPTS_DIR', str(scripts)), \
                 patch.object(voice_library_module, 'VOICE_LIBRARY_PATH', str(library)), \
                 patch.object(core_module, 'VOICE_LIBRARY_PATH', str(library)):
                response = client.post('/api/voice_library/apply_bulk', json={
                    'cast': 'series', 'mapping': {'Hero': 'hero'},
                    'script_names': ['missing', 'valid', 'directory', 'empty']})
            self.assertEqual(200, response.status_code, response.text)
            results = response.json()['results']
            self.assertEqual(['missing', 'valid', 'directory', 'empty'], [r['name'] for r in results])
            for index in (0, 2):
                self.assertEqual([], results[index]['applied'])
                self.assertEqual(0, results[index]['count'])
                self.assertIn('not found', results[index]['error'])
            self.assertEqual({'name': 'valid', 'applied': ['Hero'], 'count': 1}, results[1])
            self.assertEqual({'name': 'empty', 'applied': [], 'count': 0}, results[3])
            saved = json.loads((scripts / 'valid.voice_config.json').read_bytes())
            self.assertEqual(voice, saved['Hero'])
            self.assertEqual({'type': 'custom', 'voice': 'Ryan'}, saved['Other'])
            self.assertEqual(preserved, {p.name: p.read_bytes() for p in scripts.iterdir()
                if p.is_file() and p.name not in {'valid.voice_config.json',
                    'valid.voice_config.json.lock', 'empty.voice_config.json.lock',
                    'missing.json.lock', 'valid.json.lock', 'directory.json.lock', 'empty.json.lock',
                    '.active_book_transaction.json.lock'}})
            self.assertEqual(b'', (scripts / '.active_book_transaction.json.lock').read_bytes())
            for name in ('missing', 'valid', 'directory', 'empty'):
                self.assertEqual(b'', (scripts / f'{name}.json.lock').read_bytes())
            from tests.test_support import assert_file_lock_released
            for name in ('valid.voice_config.json', 'empty.voice_config.json'):
                assert_file_lock_released(str(scripts / name))
            self.assertTrue((scripts / 'directory.json').is_dir())
            self.assertFalse((scripts / 'empty.voice_config.json').exists())


class TranscriptContainerValidationTests(unittest.TestCase):
    def test_actual_enricher_cli_refuses_bad_shapes_and_preserves_prior_output(self):
        from unittest.mock import Mock
        fake_llama = SimpleNamespace(Llama=object, llama_supports_gpu_offload=lambda: True)
        path = Path(__file__).resolve().parent.parent.parent / "llm_enricher.py"
        with patch.dict(sys.modules, {"llama_cpp": fake_llama}):
            spec = importlib.util.spec_from_file_location("test_llm_enricher_shape", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        enricher = module.LLMEnricher.__new__(module.LLMEnricher)
        enricher._gpu_lease = None
        enricher.fields = ["emotional_tone"]
        enricher.llm = Mock(return_value={"choices": [{"text": '{"emotional_tone":"calm"}'}]})
        valid = {"text": "Keep this text.", "speaker": "Alice", "start": 0.0, "end": 1.0}
        cases = (None, True, 7, "text", {}, {"chunks": [valid]}, [None], ["text"], [7], [valid, None])
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "input.json", Path(tmp) / "output.json"
            prior = b'[{"old": true}]'
            for payload in cases:
                with self.subTest(payload=payload):
                    source.write_text(json.dumps(payload))
                    original = source.read_bytes()
                    output.write_bytes(prior)
                    enricher.llm = Mock(return_value={"choices": [{"text": '{"emotional_tone":"calm"}'}]})
                    model = enricher.llm
                    with patch.object(sys, "argv", ["llm_enricher.py", "--model-path", "unused.gguf", "--input-file", str(source), "--output-file", str(output)]), \
                         patch.object(module, "LLMEnricher", return_value=enricher), \
                         self.assertLogs(module.logger, level="ERROR") as logs:
                        with self.assertRaises(SystemExit) as raised:
                            module.main()
                    self.assertEqual(1, raised.exception.code)
                    self.assertTrue(any("list of transcript objects" in line for line in logs.output))
                    enricher.llm.assert_not_called()
                    self.assertEqual(prior, output.read_bytes())
                    self.assertEqual(original, source.read_bytes())
            for payload in ([], [valid]):
                with self.subTest(valid_payload=payload):
                    source.write_text(json.dumps(payload))
                    original = source.read_bytes()
                    enricher.llm = Mock(return_value={"choices": [{"text": '{"emotional_tone":"calm"}'}]})
                    model = enricher.llm
                    with patch.object(sys, "argv", ["llm_enricher.py", "--model-path", "unused.gguf", "--input-file", str(source), "--output-file", str(output)]), \
                         patch.object(module, "LLMEnricher", return_value=enricher):
                        module.main()
                    expected = [] if not payload else [{**valid, "emotional_tone": "calm"}]
                    self.assertEqual(expected, json.loads(output.read_text()))
                    self.assertEqual(len(payload), model.call_count)
                    model.close.assert_called_once()
                    self.assertIsNone(enricher.llm)
                    self.assertEqual(original, source.read_bytes())
