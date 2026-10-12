import copy
import contextlib
import io
import json
import tempfile
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import generate_script as generation
import review_script as review
import three_pass_generate as three_pass
from literal_policy import (bind_literal_policy, get_literal_policy,
    ensure_literal_narration, retain_literal_policy, ensure_literal_chunk,
    bind_literal_chunks, SCRIPT_POLICY_KEY, CHUNK_POLICY_KEY)
from project import group_into_chunks, ProjectManager
from speech_text import get_speech_preparation
from literal_prompts import LITERAL_RULE, ANNOTATION_SYSTEM, REVIEW_SYSTEM
from routers.script import GenerateScriptRequest, build_generate_script_command


def row(text):
    return {"speaker": "NARRATOR", "text": text, "instruct": "Neutral."}


class LiteralPipelinePolicyTests(unittest.TestCase):
    def test_binding_is_portable_immutable_and_checks_literal_source(self):
        source = "How she longed to leave."
        original = [row(source)]
        before = copy.deepcopy(original)
        marked = bind_literal_policy(source, original)
        self.assertEqual(before, original)
        self.assertEqual(get_literal_policy(marked), ensure_literal_narration(marked))
        with self.assertRaises(ValueError):
            bind_literal_policy(source, [row("How I longed to leave.")])
        damaged = copy.deepcopy(marked)
        damaged[0]["text"] = "How I longed to leave."
        with self.assertRaises(ValueError):
            ensure_literal_narration(damaged)

    def test_policy_cannot_be_conflicting_or_malformed(self):
        marked = bind_literal_policy("One word.", [row("One word.")])
        for value in [True, {}, {**marked[0][SCRIPT_POLICY_KEY], "version": "unknown"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_literal_policy([{**row("One word."), SCRIPT_POLICY_KEY: value}])

    def test_default_off_preserves_rows_and_command_and_api_type_is_strict(self):
        entries = [row("Chapter IV costs $5.")]
        self.assertIsNone(ensure_literal_narration(entries))
        self.assertIs(entries, retain_literal_policy(entries, None))
        self.assertFalse(GenerateScriptRequest().literal_source_preserving)
        self.assertNotIn("--literal-source-preserving", build_generate_script_command("book.txt"))
        self.assertIn("--literal-source-preserving", build_generate_script_command("book.txt", literal_source_preserving=True))
        with self.assertRaises(ValueError):
            GenerateScriptRequest(literal_source_preserving="true")

    def test_review_retries_changed_words_with_dedicated_prompt(self):
        text = "How she longed to leave."
        original = bind_literal_policy(text, [row(text)])
        responses = iter([json.dumps([row("How I longed to leave.")]), json.dumps([row(text)])])
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generation.LLMGenParams(literal_source_preserving=True)
        with patch.object(review, "wait_for_vram_headroom", return_value=True):
            output = review.review_batch(client, "fixture", original, 1, 1, params, max_retries=1)
        self.assertEqual(2, len(calls))
        self.assertEqual(REVIEW_SYSTEM, calls[0]["messages"][0]["content"])
        self.assertNotIn(SCRIPT_POLICY_KEY, calls[0]["messages"][1]["content"])
        retained = retain_literal_policy(output, get_literal_policy(original))
        self.assertEqual(get_literal_policy(original), get_literal_policy(retained))

    def test_review_final_guard_rejects_changes_even_after_postprocessing(self):
        original = bind_literal_policy("She left.", [row("She left.")])
        with self.assertRaises(ValueError):
            retain_literal_policy([row("I left.")], get_literal_policy(original))

    def test_review_checkpoint_isolates_policy_and_cannot_reuse_legacy(self):
        marked = bind_literal_policy("She left.", [row("She left.")])
        with tempfile.TemporaryDirectory() as td:
            output = str(Path(td) / "script.json")
            args = (output, 0, 1, 25, 0, [], {}, None, [], [], review._entries_fingerprint(marked))
            review.save_checkpoint(*args)
            self.assertIsNone(review.load_checkpoint(output, 1, 25, 0, marked))
            review.save_checkpoint(*args, literal_policy=get_literal_policy(marked))
            self.assertIsNotNone(review.load_checkpoint(output, 1, 25, 0, marked))

    def test_literal_checkpoint_refuses_a_corrupted_completed_prefix(self):
        marked = bind_literal_policy("She left.", [row("She left.")])
        with tempfile.TemporaryDirectory() as td:
            output = str(Path(td) / "script.json")
            review.save_checkpoint(output, 1, 1, 25, 0, [row("I left.")], {}, None,
                [1], [], review._entries_fingerprint(marked), source_offsets=[1],
                literal_policy=get_literal_policy(marked))
            self.assertIsNone(review.load_checkpoint(output, 1, 25, 0, marked))

    def test_three_pass_prompt_and_checkpoint_identity_select_literal_only(self):
        params = generation.LLMGenParams()
        frozen = [{"speaker": "NARRATOR", "text": "Chapter IV."}]
        legacy = three_pass.three_pass_fingerprint("Chapter IV.", "fixture", 100, params)
        ordinary_system, _ = three_pass.build_instruct_request(frozen, params)
        self.assertNotIn(LITERAL_RULE, ordinary_system)
        params.literal_source_preserving = True
        system, _ = three_pass.build_instruct_request(frozen, params)
        self.assertEqual(ordinary_system, system)
        self.assertNotEqual(legacy, three_pass.three_pass_fingerprint("Chapter IV.", "fixture", 100, params))
        self.assertIn("Do not omit attribution", ANNOTATION_SYSTEM)

    def test_indexed_scope_change_moves_checkpoint_identity_not_script_provenance(self):
        from literal_policy import PROMPT_VERSION
        from literal_prompts import CHECKPOINT_PROMPT_POLICY
        params = generation.LLMGenParams(literal_source_preserving=True)
        captured = []
        real = three_pass.json.dumps

        def capture(obj, *a, **k):
            if isinstance(obj, dict) and "literal_prompt_policy" in obj:
                captured.append(obj["literal_prompt_policy"])
            return real(obj, *a, **k)
        with patch.object(three_pass.json, "dumps", side_effect=capture):
            three_pass.three_pass_fingerprint("Chapter IV.", "fixture", 100, params)
        self.assertEqual(["literal_prompts_v2"], captured)
        self.assertEqual("literal_prompts_v2", CHECKPOINT_PROMPT_POLICY)
        # Finished scripts keep their provenance: pass 2/3 never change words.
        self.assertEqual("literal_prompts_v1", PROMPT_VERSION)

    def test_literal_indexed_prompts_keep_schema_and_reference_context(self):
        from dataclasses import replace
        frozen = [{"type": "SPOKEN", "text": "Wait for me!"}]
        prior = [{"speaker": "MARA", "text": "Wait for me!"}]
        contexts = [{"previous_context": {"speaker": "NARRATOR", "text": "Mara turned."}}]
        for custom in (False, True):
            with self.subTest(custom=custom):
                params = generation.LLMGenParams(
                    attribute_system_prompt="Custom indexed attribution." if custom else None,
                    instruct_system_prompt="Custom indexed delivery." if custom else None)
                literal = replace(params, literal_source_preserving=True)
                ordinary_attribute = three_pass.build_attribute_request(
                    frozen, params, ["MARA"], contexts, {"before": "Read-only source context."})
                self.assertEqual(ordinary_attribute, three_pass.build_attribute_request(
                    frozen, literal, ["MARA"], contexts, {"before": "Read-only source context."}))
                ordinary_instruct = three_pass.build_instruct_request(prior, params, contexts)
                self.assertEqual(ordinary_instruct, three_pass.build_instruct_request(prior, literal, contexts))
                self.assertNotIn(LITERAL_RULE, ordinary_attribute[0])
                self.assertNotIn(LITERAL_RULE, ordinary_instruct[0])

    def test_literal_attribution_presets_keep_their_own_system(self):
        import attribution_prompt_variants
        class CapturedRequest(Exception):
            pass
        cases = [("default", {"system": "Preset indexed attribution.", "user": "{batch}"}),
                 ("aliases", None)]
        for variant, texts in cases:
            with self.subTest(variant=variant):
                captured = []
                def provider(*args, **kwargs):
                    captured.append(kwargs["texts"])
                    raise CapturedRequest()
                params = generation.LLMGenParams(literal_source_preserving=True)
                with patch.object(attribution_prompt_variants, "make_provider", side_effect=provider), \
                        self.assertRaises(CapturedRequest):
                    three_pass.run_three_pass(None, "fixture", "Synthetic narration.", params, 1000,
                        attribute_prompt_variant=variant, attribute_prompt_texts=texts)
                self.assertEqual(texts, captured[0])

    def test_literal_pass1_keeps_word_policy_after_indexed_scope_change(self):
        text = "She walked away slowly."
        captured = []
        def create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            response = json.dumps([{"type": "NARRATOR", "text": text}])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=response), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generation.LLMGenParams(literal_source_preserving=True, segmentation="llm")
        with contextlib.redirect_stdout(io.StringIO()):
            entries = three_pass.segment_chunk(client, "fixture", text, params, max_retries=0)
        self.assertEqual(text, entries[0]["text"])
        self.assertIn(LITERAL_RULE, captured[0])
        self.assertIn(LITERAL_RULE, REVIEW_SYSTEM)

    def test_three_pass_segmentation_rejects_word_rewrite_without_near_miss(self):
        text = "How she longed to leave. " + " ".join("word" + str(i) for i in range(100))
        params = generation.LLMGenParams(literal_source_preserving=True)
        quality = three_pass.validate_segment_for_params(text,
            [{"type": "NARRATOR", "text": text.replace("How she", "How I")}], params)
        self.assertFalse(quality["passed"])
        self.assertIn("literal_source_word_mismatch", {f["code"] for f in quality["findings"]})

    def test_literal_chunks_allow_speech_expansion_but_reject_narration_edit(self):
        source = "She bought tea & cake."
        script = bind_literal_policy(source, [row(source)])
        chunks = group_into_chunks(script)
        self.assertTrue(chunks)
        for chunk in chunks:
            ensure_literal_chunk(chunk, get_literal_policy(script))
        prepared = get_speech_preparation(source, "Neutral.")
        self.assertNotEqual(source, prepared["text"])
        self.assertIn("and", prepared["text"])
        changed = copy.deepcopy(chunks[0])
        changed["text"] = "Invented narration."
        with self.assertRaises(ValueError):
            ensure_literal_chunk(changed, get_literal_policy(script))

    def test_tts_admission_fails_before_engine_creation_and_catches_unmarked_cache(self):
        script = bind_literal_policy("She left.", [row("She left.")])
        with tempfile.TemporaryDirectory() as td:
            manager = ProjectManager(td)
            Path(manager.script_path).write_text(json.dumps(script))
            chunks = group_into_chunks(script)
            with patch.object(manager, "get_engine") as engine:
                manager.ensure_literal_render_inputs(chunks, [0])
                chunks[0]["text"] = "I left."
                with self.assertRaises(ValueError):
                    manager.ensure_literal_render_inputs(chunks, [0])
                engine.assert_not_called()
                with self.assertRaises(ValueError):
                    manager.ensure_literal_render_inputs([row("She left.")], [0])

    def test_chunk_source_policy_is_bound_and_not_reconstructed(self):
        first = bind_literal_policy("One word.", [row("One word.")])
        second = bind_literal_policy("Different words.", [row("Different words.")])
        chunk = bind_literal_chunks([row("One word.")], get_literal_policy(first))[0]
        with self.assertRaises(ValueError):
            ensure_literal_chunk(chunk, get_literal_policy(second))
        self.assertIn(CHUNK_POLICY_KEY, chunk)

    def test_orphan_literal_chunks_refuse_when_script_policy_is_removed(self):
        script = bind_literal_policy("She left.", [row("She left.")])
        with tempfile.TemporaryDirectory() as td:
            manager = ProjectManager(td)
            Path(manager.script_path).write_text(json.dumps([row("She left.")]))
            with self.assertRaises(ValueError):
                manager.ensure_literal_render_inputs(group_into_chunks(script), [0])

    def test_real_three_pass_flow_publishes_policy_on_frozen_narration(self):
        text = "She walked away slowly."
        responses = iter([json.dumps([{"n": 0, "head": text, "instruct": "Neutral."}])])
        def create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generation.LLMGenParams(literal_source_preserving=True, segmentation="quotes")
        with contextlib.redirect_stdout(io.StringIO()):
            output = three_pass.run_three_pass(client, "fixture", text, params, 1000)
        self.assertIsNotNone(ensure_literal_narration(output))
        self.assertEqual(text, " ".join(e["text"] for e in output))

    def test_compact_and_literal_flags_gate_reconstructed_entries(self):
        import compact_pass1
        text = "She walked away slowly."
        def create(**kwargs):
            reply = json.dumps([{"n": 1, "type": "NARRATOR"}])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generation.LLMGenParams(literal_source_preserving=True, pass1_format="compact")
        with contextlib.redirect_stdout(io.StringIO()):
            entries = compact_pass1.segment_chunk_compact(client, "fixture", text, params)
            self.assertEqual(text, " ".join(e["text"] for e in entries))
            with patch.object(compact_pass1, "get_compact_entries", return_value={
                    "entries": [{"type": "NARRATOR", "text": "I walked away slowly."}], "unresolved": []}):
                self.assertEqual([], compact_pass1.segment_chunk_compact(client, "fixture", text, params))

    def test_compact_literal_flow_preserves_policy_on_success_and_fallback(self):
        text = "She walked away slowly."
        for reply, fallback in [(json.dumps([{"n": 1, "type": "NARRATOR"}]), False), ("[]", True)]:
            with self.subTest(fallback=fallback):
                def create(**kwargs):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply), finish_reason="stop")], usage=None)
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                params = generation.LLMGenParams(literal_source_preserving=True, pass1_format="compact", segmentation="llm")
                with patch.object(three_pass, "segment_chunk_adaptively", return_value=[{"type": "NARRATOR", "text": text}]) as ordinary, \
                        contextlib.redirect_stdout(io.StringIO()):
                    output = three_pass.run_three_pass(client, "fixture", text, params, 1000)
                self.assertEqual(1 if fallback else 0, ordinary.call_count)
                self.assertIsNotNone(ensure_literal_narration(output))
                self.assertEqual(text, " ".join(e["text"] for e in output))

    def test_compact_literal_fallback_split_halves_reject_four_missing_words(self):
        left = " ".join(f"left{i}" for i in range(200))
        right = " ".join(f"right{i}" for i in range(200))
        text = left + "\n\n" + right
        requests = []
        def create(**kwargs):
            content = kwargs["messages"][-1]["content"]
            requests.append(content)
            if "left199" in content and "right199" in content:
                entries = []  # Compact and the full ordinary attempt both exhaust.
            elif "left199" in content:
                entries = [{"type": "NARRATOR", "text": " ".join(left.split()[4:])}]
            else:
                entries = [{"type": "NARRATOR", "text": right}]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(entries)), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generation.LLMGenParams(literal_source_preserving=True, pass1_format="compact", segmentation="llm", segment_user_prompt_template="{chunk}")
        with tempfile.TemporaryDirectory() as td:
            output = str(Path(td) / "script.json")
            with patch.object(three_pass, "split_failed_chunk", side_effect=lambda chunk: [left, right] if chunk == text else []), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(RuntimeError, "pass 1 .* failed"):
                three_pass.run_three_pass(client, "fixture", text, params, len(text) + 100, output_path=output)
            self.assertTrue(any("left199" in c and "right199" not in c for c in requests))
            self.assertTrue(any("right199" in c and "left199" not in c for c in requests))
            self.assertFalse(Path(output).exists())
            manifest = json.loads(Path(three_pass.three_pass_manifest_path(output)).read_text())
            self.assertEqual("failed", manifest["status"])
            self.assertIn("literal_source_word_mismatch", json.dumps(manifest))

    def test_compact_literal_checkpoint_and_policy_identities_are_distinct(self):
        fingerprints = set()
        real_dumps = json.dumps
        for fmt in ("ordinary", "compact"):
            for literal in (False, True):
                captured = []
                def capture(obj, *args, **kwargs):
                    if isinstance(obj, dict) and "pipeline_version" in obj:
                        captured.append(copy.deepcopy(obj))
                    return real_dumps(obj, *args, **kwargs)
                params = generation.LLMGenParams(pass1_format=fmt, literal_source_preserving=literal)
                with patch.object(three_pass.json, "dumps", side_effect=capture):
                    fingerprints.add(real_dumps(three_pass.three_pass_fingerprint("One word.", "fixture", 1000, params), sort_keys=True))
                settings = captured[-1]
                self.assertEqual(fmt == "compact", "pass1_format" in settings)
                self.assertEqual(literal, "literal_source_preserving" in settings)
                self.assertEqual(literal, "literal_prompt_policy" in settings)
        self.assertEqual(4, len(fingerprints))
        policy = ensure_literal_narration(bind_literal_policy("One word.", [row("One word.")]))
        self.assertEqual("literal_words_v1", policy["version"])
        self.assertEqual("literal_prompts_v1", policy["prompt_version"])

    def test_review_cli_inherits_policy_and_retains_it_in_published_output(self):
        text = "She walked away slowly."
        source = bind_literal_policy(text, [row(text)])
        def create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([row(text)])), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        status = {"context_length": 8192, "parallel": 1, "loaded": False, "optimized": False}
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "input.json"
            output_path = Path(td) / "output.json"
            input_path.write_text(json.dumps(source))
            with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": td, "ALEXANDRIA_CONFIG_PATH": td + "/config.json"}), \
                    patch.object(sys, "argv", ["review_script.py", "--input", str(input_path), "--output", str(output_path)]), \
                    patch.object(review, "ensure_ideal_settings", return_value=(False, status, "CPU fixture")), \
                    patch.object(review, "get_current_status", return_value=status), \
                    patch.object(review, "make_run_client", return_value=client), \
                    patch.object(review, "get_cached_or_benchmarked_concurrency", return_value=1), \
                    patch.object(review, "wait_for_vram_headroom", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                review.main()
            published = json.loads(output_path.read_text())
            self.assertEqual(get_literal_policy(source), ensure_literal_narration(published))
            self.assertEqual(source, json.loads(input_path.read_text()))
