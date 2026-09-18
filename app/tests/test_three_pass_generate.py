import re
import unittest
import os
import tempfile
import three_pass_generate as tp
import json
from types import SimpleNamespace
from unittest.mock import patch
from generate_script import LLMGenParams
from script_repair import build_deterministic_repair


class PassHelperTests(unittest.TestCase):
    def test_unique_batches_isolate_duplicates_without_collapsing_prefix(self):
        seg = [{"type": "SPOKEN", "text": "Yes."},
               {"type": "SPOKEN", "text": "Yes."},
               {"type": "NARRATOR", "text": "He left the room."},
               {"type": "SPOKEN", "text": "No."}]
        batches = list(tp.iter_unique_entry_batches(seg))
        self.assertEqual([[0, 2, 3], [1]],
                         [[index for index, _ in batch] for batch in batches])

    def test_unique_batches_cap_source_windows_at_batch_size(self):
        seg = [{"type": "NARRATOR", "text": f"line {i}"} for i in range(60)]
        self.assertEqual([25, 25, 10],
                         [len(batch) for batch in tp.iter_unique_entry_batches(seg)])

    def test_resolve_chunk_size_cli_overrides_config(self):
        self.assertEqual(3000, tp.resolve_chunk_size(3000, 6000))
        self.assertEqual(6000, tp.resolve_chunk_size(None, 6000))
        self.assertEqual(2500, tp.resolve_chunk_size(None, 3000, 2500))

    def test_resolve_chunk_size_rejects_bad_config_value(self):
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(None, 0)      # bad config value now caught
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(-5, 6000)     # bad CLI value still caught
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(None, "big")  # non-int config

    def test_roster_collects_uppercase_non_narrator_speakers(self):
        entries = [{"speaker": "NARRATOR"}, {"speaker": "ELENA"},
                   {"speaker": "MARCUS"}, {"speaker": "ELENA"}, {"speaker": "UNKNOWN"}]
        self.assertEqual(["ELENA", "MARCUS"], tp.build_roster(entries))

    def test_attribute_specific_prompt_does_not_override_other_passes(self):
        params = LLMGenParams(
            max_tokens=500, system_prompt="shared override",
            attribute_system_prompt="attribute override")

        attribute_system, _ = tp.build_attribute_request(
            [{"type": "SPOKEN", "text": "Hello."}], params, ["ALICE"])
        instruct_system, _ = tp.build_instruct_request(
            [{"speaker": "ALICE", "text": "Hello."}], params)

        self.assertEqual("attribute override", attribute_system)
        self.assertEqual("shared override", instruct_system)

    def test_default_instruct_by_type(self):
        self.assertEqual("Neutral, even narration.",
                         tp.default_instruct({"speaker": "NARRATOR", "text": "x"}))
        self.assertEqual("Natural, in-character delivery.",
                         tp.default_instruct({"speaker": "ELENA", "text": "x"}))

    def test_outer_quote_regions_and_preflight_selection(self):
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She said,"},
             {"type": "SPOKEN", "text": "Go."}],
            tp.split_outer_quote_regions('She said, "Go."'))
        source = "\n\n".join(["first " * 1000, "middle " * 1000,
                               'She said "dialogue." ' * 400])
        selected = tp.select_preflight_chunks(source, 3000)
        self.assertEqual("first", selected[0][0])
        self.assertIn("dialogue", {label for label, _, _ in selected})

    def test_preflight_dialogue_selection_ignores_quote_dense_endnotes(self):
        prose = ('She said “One.” He answered “Two.” ' * 50).strip()
        notes = ('Reference “one” ←1. Note “two” ←2. Note “three” ←3. ' * 40).strip()
        source = "plain opening\n\n" + prose + "\n\n" + notes
        selected = {label: chunk for label, _, chunk in
                    tp.select_preflight_chunks(source, 1000)}
        self.assertIn("She said", selected["dialogue"])
        self.assertLessEqual(selected["dialogue"].count("←"), 2)

    def test_outer_quote_regions_support_curly_quotes(self):
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She said,"},
             {"type": "SPOKEN", "text": "Go."}],
            tp.split_outer_quote_regions("She said, “Go.”"))

    def test_unmatched_outer_quote_is_bounded_at_input_end(self):
        analysis = tp.analyze_outer_quote_regions('She said, "Go.')
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She said,"},
             {"type": "SPOKEN", "text": "Go."}], analysis["regions"])
        self.assertEqual("inferred_missing_close_quote",
                         analysis["repairs"][0]["code"])

    def test_nested_curly_quotes_keep_outer_dialogue_boundary(self):
        source = 'Subaru “My plan to “impress everyone” has failed.” Emilia nodded.'
        self.assertEqual(
            [{"type": "SPOKEN", "text": "My plan to impress everyone has failed.",
              "source_label": "Subaru"},
             {"type": "NARRATOR", "text": "Emilia nodded."}],
            tp.split_outer_quote_regions(source))

    def test_inner_curly_open_can_share_outer_close(self):
        source = 'Emilia “I have to say this “I will not do that!”\n\nThey left.'
        self.assertEqual(
            [{"type": "SPOKEN", "text": "I have to say this I will not do that!",
              "source_label": "Emilia"},
             {"type": "NARRATOR", "text": "They left."}],
            tp.split_outer_quote_regions(source))

    def test_japanese_quote_pairs_are_spoken_regions(self):
        source = 'She read 「come home」 and then 『stay safe』 aloud.'
        entries = tp.split_outer_quote_regions(source)
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She read"},
             {"type": "SPOKEN", "text": "come home"},
             {"type": "NARRATOR", "text": "and then"},
             {"type": "SPOKEN", "text": "stay safe"},
             {"type": "NARRATOR", "text": "aloud."}], entries)
        self.assertTrue(tp.validate_segment_quality(source, entries)["passed"])

    def test_nested_japanese_quotes_keep_outer_boundary(self):
        source = 'He said 「read 『this』 now」 quietly.'
        self.assertEqual(
            [{"type": "NARRATOR", "text": "He said"},
             {"type": "SPOKEN", "text": "read this now"},
             {"type": "NARRATOR", "text": "quietly."}],
            tp.split_outer_quote_regions(source))

    def test_explicit_source_speaker_label_attaches_to_spoken_region(self):
        source = 'Narration.\n\nBeatrice “Took it? From the Witch Cult?”'
        entries = tp.split_outer_quote_regions(source)
        self.assertEqual(
            [{"type": "NARRATOR", "text": "Narration."},
             {"type": "SPOKEN", "text": "Took it? From the Witch Cult?",
              "source_label": "Beatrice"}], entries)
        self.assertTrue(tp.validate_segment_quality(source, entries)["passed"])

    def test_reporting_clause_is_not_treated_as_source_speaker_label(self):
        source = 'She said “Come home.”'
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She said"},
             {"type": "SPOKEN", "text": "Come home."}],
            tp.split_outer_quote_regions(source))

    def test_all_caps_heading_is_not_treated_as_source_speaker_label(self):
        source = 'THE SELF-DECLARED KNIGHT AND “THE FINEST OF KNIGHTS”'
        self.assertNotIn("source_label", tp.split_outer_quote_regions(source)[0])

    def test_explicit_label_and_narration_bypass_attribution(self):
        self.assertEqual(
            {"speaker": "BEATRICE", "text": "Took it?"},
            tp.get_deterministic_named_entry(
                {"type": "SPOKEN", "text": "Took it?",
                 "source_label": "Beatrice"}))
        self.assertEqual(
            {"speaker": "NARRATOR", "text": "She paused."},
            tp.get_deterministic_named_entry(
                {"type": "NARRATOR", "text": "She paused."}))
        self.assertIsNone(tp.get_deterministic_named_entry(
            {"type": "SPOKEN", "text": "Who said that?"}))

    def test_nonverbal_spoken_entry_bypasses_attribution(self):
        self.assertEqual(
            {"speaker": "NARRATOR", "text": ".......!"},
            tp.get_deterministic_named_entry(
                {"type": "SPOKEN", "text": ".......!"}))

    def test_instruction_context_preflight_requires_response_headroom(self):
        small = [{"speaker": "A", "text": "Wait."}]
        large = [{"speaker": "A", "text": "word " * 1400} for _ in range(4)]
        params = LLMGenParams(max_tokens=10000, temperature=0.1,
                              context_length=8192, hard_max_tokens=16384)
        self.assertTrue(tp.does_instruct_batch_fit_context(small, params))
        self.assertFalse(tp.does_instruct_batch_fit_context(large, params))

    def test_missing_open_quote_after_reporting_verb_is_recovered(self):
        source = 'She quietly murmured I see…”, keeping her eyes lowered.'
        analysis = tp.analyze_outer_quote_regions(source)
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She quietly murmured"},
             {"type": "SPOKEN", "text": "I see…"},
             {"type": "NARRATOR", "text": ", keeping her eyes lowered."}],
            analysis["regions"])
        self.assertEqual("inferred_missing_open_quote",
                         analysis["repairs"][0]["code"])
        self.assertTrue(tp.validate_segment_quality(
            source, analysis["regions"])["passed"])

    def test_missing_open_quote_after_comma_reporting_verb_is_recovered(self):
        source = 'He quietly whispered, Echidna”, then looked away.'
        analysis = tp.analyze_outer_quote_regions(source)
        self.assertEqual("Echidna", analysis["repairs"][0]["text"])
        self.assertTrue(tp.validate_segment_quality(
            source, analysis["regions"])["passed"])

    def test_ambiguous_stray_closing_quote_drops_only_delimiter(self):
        source = 'She considered I see…” and left.'
        analysis = tp.analyze_outer_quote_regions(source)
        self.assertEqual("ignored_unmatched_close_quote",
                         analysis["repairs"][0]["code"])
        self.assertEqual("She considered I see… and left.",
                         analysis["regions"][0]["text"])
        self.assertTrue(tp.validate_segment_quality(
            source, analysis["regions"])["passed"])

    def test_unclosed_quote_carries_across_paragraphs_until_input_end(self):
        source = 'She said “Stay here.\n\nHe left.'
        analysis = tp.analyze_outer_quote_regions(source)
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She said"},
             {"type": "SPOKEN", "text": "Stay here."},
             {"type": "SPOKEN", "text": "He left."}],
            analysis["regions"])
        self.assertEqual("inferred_missing_close_quote",
                         analysis["repairs"][0]["code"])

    def test_multi_paragraph_dialogue_supports_repeated_curly_openers(self):
        source = '“First paragraph.\n\n“Second paragraph.\n\n“Final paragraph.” Tail.'
        self.assertEqual(
            [{"type": "SPOKEN", "text": "First paragraph."},
             {"type": "SPOKEN", "text": "Second paragraph."},
             {"type": "SPOKEN", "text": "Final paragraph."},
             {"type": "NARRATOR", "text": "Tail."}],
            tp.split_outer_quote_regions(source))

    def test_multi_paragraph_dialogue_supports_unquoted_continuations(self):
        source = '"There are two tasks.\n\nFind Gisu.\n\nFind Ruijerd.\n\nUnderstood?" Tail.'
        entries = tp.split_outer_quote_regions(source)
        self.assertEqual(["SPOKEN", "SPOKEN", "SPOKEN", "SPOKEN", "NARRATOR"],
                         [entry["type"] for entry in entries])

    def test_emotional_laughter_with_decorative_closers_stays_spoken(self):
        source = 'She laughed. “Ha!”Haha!”Hahaha!” Then she stopped.'
        self.assertEqual(
            [{"type": "NARRATOR", "text": "She laughed."},
             {"type": "SPOKEN", "text": "Ha!Haha!Hahaha!"},
             {"type": "NARRATOR", "text": "Then she stopped."}],
            tp.split_outer_quote_regions(source))

    def test_repaired_quote_resolution_is_counted(self):
        counts = tp._resolution_counts(
            ["quote_presegmented", "quote_presegmented_repaired"])
        self.assertEqual(1, counts["quote_repairs"])

    def test_oversized_quoted_paragraph_carries_quote_state(self):
        source = 'Narrator “' + ('spoken words ' * 30) + 'finished.” Tail.'
        records = tp.split_into_chunk_records(source, max_size=100)
        self.assertTrue(all(len(record["text"]) <= 100 for record in records))
        self.assertTrue(records[0]["continues_paragraph_to_next"])
        self.assertTrue(records[1]["continues_paragraph_from_previous"])
        depth = 0
        recovered = []
        for record in records:
            initial = depth if record["continues_paragraph_from_previous"] else 0
            analysis = tp.analyze_outer_quote_regions(
                record["text"], initial_depth=initial,
                allow_open_end=record["continues_paragraph_to_next"])
            self.assertTrue(analysis["regions"])
            self.assertTrue(tp.validate_segment_quality(
                record["text"], analysis["regions"],
                quote_analysis=analysis)["passed"])
            recovered.extend(analysis["regions"])
            depth = analysis["final_depth"]
        self.assertEqual(0, depth)
        self.assertIn("spoken words", " ".join(
            entry["text"] for entry in recovered if entry["type"] == "SPOKEN"))

    def test_context_rescue_is_not_used_for_omission_or_quote_structure(self):
        self.assertFalse(tp.should_rescue_with_context({"low_source_token_recall"}))
        self.assertFalse(tp.should_rescue_with_context({"crosses_quote_boundary"}))
        self.assertTrue(tp.should_rescue_with_context({"context_required"}))

    def test_quote_presegmentation_needs_no_llm_rewrite(self):
        class NoCalls:
            @property
            def chat(self):
                raise AssertionError("quote pre-segmentation should be deterministic")
        source = 'Ilya said. "Stay behind me."'
        out = tp.segment_chunk_adaptively(
            NoCalls(), "m", source, LLMGenParams(segmentation="auto"))
        self.assertEqual(tp.split_outer_quote_regions(source), out)


class SegmentationModeTests(unittest.TestCase):
    """The three-way pass-1 knob (issue #588). "quotes" never calls the model;
    "auto" keeps the measured rule; "llm" never pre-segments; and the
    checkpoint identity of every auto/llm run is unchanged."""

    class NoCalls:
        @property
        def chat(self):
            raise AssertionError("pass 1 must not call the model in this mode")

    def test_quotes_mode_narrates_an_unquoted_chunk_without_a_call(self):
        sink = []
        out = tp.segment_chunk_adaptively(
            self.NoCalls(), "m", "The road was long and nobody spoke.",
            LLMGenParams(segmentation="quotes"), resolution_sink=sink)
        self.assertEqual([{"type": "NARRATOR", "text": "The road was long and nobody spoke."}], out)
        self.assertEqual(["quote_forced"], sink)

    def test_quotes_mode_keeps_the_regions_when_the_gate_declines(self):
        # a lone opening mark with no close: the analyzer repairs it, the gate
        # would send auto to the model; quotes keeps what the marks say
        source = 'He said "come here and nothing followed'
        analysis = tp.analyze_outer_quote_regions(source)
        self.assertTrue(analysis["repairs"])
        sink = []
        out = tp.segment_chunk_adaptively(
            self.NoCalls(), "m", source, LLMGenParams(segmentation="quotes"),
            resolution_sink=sink)
        self.assertEqual([e["type"] for e in out], ["NARRATOR", "SPOKEN"])
        self.assertIn(sink[0], ("quote_forced", "quote_presegmented_repaired"))

    def test_quotes_mode_marks_a_continued_quote_as_spoken(self):
        analysis = tp.analyze_outer_quote_regions("still talking here.", initial_depth=1,
                                                  allow_open_end=True)
        regions, resolution = tp.quote_regions_decision("quotes", "still talking here.", analysis)
        self.assertEqual("SPOKEN", regions[0]["type"])

    def test_auto_mode_still_asks_the_model_for_an_unquoted_chunk(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(
                    [{"type": "NARRATOR", "text": "The road was long and nobody spoke."}])),
                finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        out = tp.segment_chunk_adaptively(
            client, "m", "The road was long and nobody spoke.",
            LLMGenParams(segmentation="auto", max_tokens=500))
        self.assertEqual(1, len(calls))
        self.assertEqual("NARRATOR", out[0]["type"])

    def test_llm_mode_never_presegments(self):
        self.assertEqual((None, None), tp.quote_regions_decision(
            "llm", 'A. "B." C.', tp.analyze_outer_quote_regions('A. "B." C.')))

    def test_fingerprint_is_unchanged_for_auto_and_llm_and_new_for_quotes(self):
        auto = tp.three_pass_fingerprint("text", "m", 3000, LLMGenParams(segmentation="auto"))
        llm = tp.three_pass_fingerprint("text", "m", 3000, LLMGenParams(segmentation="llm"))
        quotes = tp.three_pass_fingerprint("text", "m", 3000, LLMGenParams(segmentation="quotes"))
        self.assertNotEqual(auto, llm)
        self.assertNotEqual(auto, quotes)
        self.assertNotEqual(llm, quotes)
        # the identity every checkpoint before the mode existed was written
        # with: the bool `presegment_quotes`, and no `segmentation` key at all
        with patch.object(tp, "load_segment_prompts", return_value=("s", "u")), \
             patch.object(tp, "load_attribute_prompts", return_value=("s", "u")), \
             patch.object(tp, "load_instruct_prompts", return_value=("s", "u")):
            for mode, legacy in (("auto", True), ("llm", False)):
                seen = {}
                real = json.dumps

                def spy(obj, **kw):
                    seen.update(obj) if isinstance(obj, dict) and "presegment_quotes" in obj else None
                    return real(obj, **kw)
                with patch.object(tp.json, "dumps", side_effect=spy):
                    tp.three_pass_fingerprint("text", "m", 3000, LLMGenParams(segmentation=mode))
                self.assertEqual(legacy, seen["presegment_quotes"])
                self.assertNotIn("segmentation", seen)

    def test_preflight_reports_what_pass_1_will_cost(self):
        text = 'Narration. "Spoken words." More narration.\n\n' + "Only narration here. " * 200
        settings = {"chunk_size": 3000, "max_tokens": 4096,
                    "segment_output_ratio": 3.0, "segmentation": "auto"}
        report = tp.build_three_pass_request_preflight(text, settings, 0, 1)["segmentation"]
        self.assertEqual("auto", report["mode"])
        self.assertEqual(report["chunks"], report["quote_presegmented"] + report["llm_chunks"])
        self.assertGreaterEqual(report["llm_chunks"], 1)
        self.assertGreaterEqual(report["chunks_without_quote_marks"], 1)
        forced = tp.build_three_pass_request_preflight(
            text, dict(settings, segmentation="quotes"), 0, 1)["segmentation"]
        self.assertEqual(0, forced["llm_chunks"])


def _client_returning(payloads):
    """LM Studio stub: each call returns the next payload as JSON content."""
    responses = iter(payloads)

    def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


class Pass2Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                            max_tokens=500, temperature=0.1)

    def test_provider_response_without_choices_is_bounded_error(self):
        from generate_script import call_llm_for_entries

        class EmptyResponse:
            def create(self, **_kwargs):
                return SimpleNamespace(choices=None, usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=EmptyResponse()))
        out = call_llm_for_entries(
            client, "m", "system", "user", self._params(),
            log_name="test_llm_responses.log", label="TEST", max_retries=1)
        self.assertEqual([], out)

    def test_attributes_a_batch_and_freezes_text(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        good = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                {"n": 1, "head": "Tell me.", "speaker": "ELENA"}]
        client = _client_returning([good])
        out = tp.attribute_batch(client, "m", frozen, self._params(), roster=[])
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in out])
        self.assertEqual("Tell me.", out[1]["text"])

    def test_attribute_prompt_includes_read_only_neighbor_context(self):
        seen = {}
        good = [{"n": 0, "head": "Yes", "speaker": "ELENA"}]
        def create(**kwargs):
            seen["prompt"] = kwargs["messages"][-1]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(good)), finish_reason="stop")],
                usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": "Yes."}],
                           self._params(), roster=[], neighbor_contexts=[{
                               "previous_context": {"type": "SPOKEN", "text": "Did you?"},
                               "next_context": {"type": "NARRATOR", "text": "She nodded."}}])
        self.assertIn("Did you?", seen["prompt"])
        self.assertIn("She nodded.", seen["prompt"])

    def test_pass2_fail_mode_raises_when_exhausted(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]
        client = _client_returning([bad, bad, bad, bad])
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, "m", frozen, self._params(), roster=[],
                               max_retries=1, on_exhaustion="fail")


class Pass3Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                            max_tokens=500, temperature=0.1)

    def test_adds_instruct_and_freezes(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        good = [{"n": 0, "head": "Tell me.", "instruct": "Firm, quiet."}]
        client = _client_returning([good])
        out = tp.instruct_batch(client, "m", prior, self._params())
        self.assertEqual("Firm, quiet.", out[0]["instruct"])

    def test_falls_back_to_default_instruct_on_exhaustion(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold."}]
        bad = [{"n": 0, "head": "The room was", "instruct": ""}]
        client = _client_returning([bad, bad])
        out = tp.instruct_batch(client, "m", prior, self._params(), max_retries=1)
        self.assertEqual("Neutral, even narration.", out[0]["instruct"])
        self.assertEqual("The room was cold.", out[0]["text"])


class EndToEndTests(unittest.TestCase):
    def test_repeated_omission_stops_same_chunk_retry_early(self):
        source = " ".join(f"word{i}" for i in range(100))
        short = [{"type": "NARRATOR", "text": " ".join(source.split()[:20])}]
        calls = {"count": 0}
        def create(**_kwargs):
            calls["count"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(short)),
                finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        self.assertEqual([], tp.segment_chunk(
            client, "m", source, LLMGenParams(max_tokens=500), max_retries=4))
        self.assertEqual(2, calls["count"])

    def test_segment_strips_standalone_spoken_quote_delimiters(self):
        source = 'She asked, "Did you hear that?"'
        response = [{"type": "NARRATOR", "text": "She asked,"},
                    {"type": "SPOKEN", "text": '"Did you hear that?"'}]
        out = tp.segment_chunk(_client_returning([response]), "m", source,
                               LLMGenParams(max_tokens=500, temperature=0.1))
        self.assertEqual("Did you hear that?", out[1]["text"])

    def test_segment_splits_narrator_entry_containing_quoted_dialogue(self):
        source = 'Ilya said. "Stay behind me."'
        response = [{"type": "NARRATOR", "text": source}]
        out = tp.segment_chunk(_client_returning([response]), "m", source,
                               LLMGenParams(max_tokens=500, temperature=0.1))
        self.assertEqual(
            [{"type": "NARRATOR", "text": "Ilya said."},
             {"type": "SPOKEN", "text": "Stay behind me."}], out)

    def test_segment_completion_budget_is_bounded_from_source_size(self):
        seen = {}
        source = " ".join(f"word{i}" for i in range(100))
        response = [{"type": "NARRATOR", "text": source}]
        def create(**kwargs):
            seen["max_tokens"] = kwargs["max_tokens"]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(response)),
                finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        out = tp.segment_chunk(client, "m", source,
                               LLMGenParams(max_tokens=10000, hard_max_tokens=16384))
        self.assertTrue(out)
        self.assertEqual(512, seen["max_tokens"])

    def test_attribution_exhaustion_subdivides_and_completes(self):
        source = "One line. Two line."
        seg = [{"type": "SPOKEN", "text": "One line."},
               {"type": "SPOKEN", "text": "Two line."}]
        bad_pair = [{"n": 0, "speaker": "NARRATOR"},
                    {"n": 1, "speaker": "NARRATOR"}]
        first = [{"n": 0, "speaker": "ALICE"}]
        second = [{"n": 0, "speaker": "BOB"}]
        instructed = [{"n": 0, "instruct": "Quiet."},
                      {"n": 1, "instruct": "Firm."}]
        client = _client_returning([seg] + [bad_pair] * 4 +
                                   [first, second, instructed])
        entries = tp.run_three_pass(client, "m", source,
                                    LLMGenParams(max_tokens=500, temperature=0.1),
                                    chunk_size=6000)
        self.assertEqual(["ALICE", "BOB"], [e["speaker"] for e in entries])

    def test_three_passes_assemble_final_entries(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        # Narration is now sent with the batch as context, so pass 2
        # answers for both entries; its answer for the narration line is
        # discarded in favour of the deterministic NARRATOR.
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold, still narration."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm, quiet demand."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000)
        self.assertEqual(2, len(entries))
        self.assertEqual({"speaker", "text", "instruct"}, set(entries[0].keys()))
        self.assertEqual("ELENA", entries[1]["speaker"])
        self.assertEqual("Firm, quiet demand.", entries[1]["instruct"])

    def test_first_person_narrator_is_seeded_into_attribution_roster(self):
        source = "Alexis spoke. Alexis waited. Alexis answered."
        segmented = [{"type": "SPOKEN", "text": source}]
        named = [{"speaker": "ALEXIS", "text": source}]
        instructed = [{"speaker": "ALEXIS", "text": source,
                       "instruct": "Reflective."}]
        observed = {}

        def attribute(*args, **kwargs):
            observed["roster"] = list(kwargs["roster"])
            return named, [1.0]

        with patch.object(tp, "segment_chunk_adaptively",
                          return_value=segmented), \
             patch.object(tp, "attribute_batch_voted", side_effect=attribute), \
             patch.object(tp, "instruct_batch", return_value=instructed):
            entries = tp.run_three_pass(
                object(), "m", source,
                LLMGenParams(max_tokens=500, temperature=0.1),
                chunk_size=6000, first_person_narrator="alexis")

        self.assertEqual("ALEXIS", observed["roster"][0])
        self.assertEqual("ALEXIS", entries[0]["speaker"])

    def test_fallback_roster_rebuild_keeps_first_person_narrator(self):
        segmented = [{"type": "SPOKEN", "text": f"Alexis line {i}."}
                     for i in range(26)]
        source = " ".join(entry["text"] for entry in segmented)
        observed_rosters = []

        def attribute(_client, _model, batch, _params, **kwargs):
            observed_rosters.append(list(kwargs["roster"]))
            return ([{"speaker": "UNKNOWN", "text": entry["text"]}
                     for entry in batch], [1.0] * len(batch))

        def instruct(_client, _model, batch, _params, **_kwargs):
            return [{**entry, "instruct": "Natural."} for entry in batch]

        with patch.object(tp, "segment_chunk_adaptively",
                          return_value=segmented), \
             patch.object(tp, "attribute_batch_voted", side_effect=attribute), \
             patch.object(tp, "instruct_batch", side_effect=instruct):
            tp.run_three_pass(
                object(), "m", source,
                LLMGenParams(max_tokens=500, temperature=0.1),
                chunk_size=6000, on_exhaustion="fallback",
                first_person_narrator="alexis")

        self.assertEqual(2, len(observed_rosters))
        self.assertTrue(all(roster[0] == "ALEXIS"
                            for roster in observed_rosters))

    def test_fallback_mode_completes_and_rebuilds_roster(self):
        # Exercises on_exhaustion="fallback": an unnameable SPOKEN line degrades
        # to UNKNOWN (no PassExhausted), and the pass-2 loop takes the
        # roster-rebuild branch (finding #15) without crashing.
        source = "Hi there friend."
        seg = [{"type": "SPOKEN", "text": "Hi there friend."}]
        bad = [{"n": 0, "head": "Hi there friend", "speaker": "NARRATOR"}]  # never names it
        instr = [{"n": 0, "head": "Hi there friend", "instruct": "z"}]
        client = _client_returning([seg, bad, bad, bad, bad, instr])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000,
                                    on_exhaustion="fallback")
        self.assertEqual(1, len(entries))
        self.assertEqual("UNKNOWN", entries[0]["speaker"])

    def test_segment_accepts_trigram_only_near_miss_on_exhaustion(self):
        words = [f"word{i}" for i in range(100)]
        source = " ".join(words)
        self.assertEqual([], tp.split_failed_chunk(source))  # unsplittable
        swapped = list(words)
        i = 0
        while i + 1 < len(swapped):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            i += 25
        near = [{"type": "NARRATOR", "text": " ".join(swapped)}]
        client = _client_returning([near, near, near, near, near, near, near])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertTrue(out)
        self.assertFalse(tp.validate_segment_quality(source, out)["passed"])


class CheckpointTests(unittest.TestCase):
    def test_nonverbal_entry_completes_without_pass2_or_pass3_call(self):
        source = '"..."'
        segmented = [{"type": "SPOKEN", "text": "..."}]
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with patch.object(tp, "segment_chunk_adaptively", return_value=segmented), \
             patch.object(tp, "attribute_batch") as attribute, \
             patch.object(tp, "instruct_batch") as instruct:
            entries = tp.run_three_pass(
                _client_returning([]), "m", source, params, chunk_size=6000)
        attribute.assert_not_called()
        instruct.assert_not_called()
        self.assertEqual("...", entries[0]["text"])

    def test_collect_all_records_segment_failure_and_continues(self):
        source = "First paragraph.\n\nSecond paragraph."
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        successful = [{"type": "NARRATOR", "text": "Second paragraph."}]
        with tempfile.TemporaryDirectory() as d, \
             patch.object(
                 tp, "segment_chunk_adaptively", side_effect=[None, successful]), \
             patch.object(tp, "should_rescue_with_context", return_value=False):
            out = os.path.join(d, "book.json")
            entries = tp.run_three_pass(
                _client_returning([]), "m", source, params, chunk_size=18,
                output_path=out, collect_all_failures=True)
            manifest = json.load(open(tp.three_pass_manifest_path(out)))
        self.assertEqual("incomplete", manifest["status"])
        self.assertIn("segment", [f["pass"] for f in manifest["diagnostic_failures"]])
        self.assertEqual(2, manifest["progress"]["chunks_attempted"])
        self.assertEqual(1, manifest["progress"]["chunks_completed"])
        self.assertEqual("Second paragraph.", entries[0]["text"])

    def test_collect_all_records_each_singleton_attribution_failure(self):
        source = '"First." "Second."'
        segmented = [{"type": "SPOKEN", "text": "First."},
                     {"type": "SPOKEN", "text": "Second."}]
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d, \
             patch.object(tp, "segment_chunk_adaptively", return_value=segmented), \
             patch.object(tp, "attribute_batch", side_effect=tp.PassExhausted):
            out = os.path.join(d, "book.json")
            entries = tp.run_three_pass(
                _client_returning([]), "m", source, params, chunk_size=6000,
                output_path=out, collect_all_failures=True)
            manifest = json.load(open(tp.three_pass_manifest_path(out)))
        failures = manifest["diagnostic_failures"]
        self.assertEqual([0, 1], [f["entry"] for f in failures])
        self.assertEqual([], entries)

    def test_collect_all_subdivides_instruction_failures_to_singletons(self):
        source = "First. Second."
        segmented = [{"type": "NARRATOR", "text": "First."},
                     {"type": "NARRATOR", "text": "Second."}]
        params = LLMGenParams(max_tokens=500, temperature=0.1)

        def exhaust_instruct(client, model, batch, params, neighbor_contexts=None,
                             exhaustion_sink=None, attempt_observer=None):
            exhaustion_sink.append(True)
            return [{**entry, "instruct": tp.default_instruct(entry)} for entry in batch]

        with tempfile.TemporaryDirectory() as d, \
             patch.object(tp, "segment_chunk_adaptively", return_value=segmented), \
             patch.object(tp, "instruct_batch", side_effect=exhaust_instruct):
            out = os.path.join(d, "book.json")
            tp.run_three_pass(_client_returning([]), "m", source, params,
                              chunk_size=6000, output_path=out,
                              collect_all_failures=True)
            manifest = json.load(open(tp.three_pass_manifest_path(out)))
        failures = [f for f in manifest["diagnostic_failures"]
                    if f["pass"] == "instruct"]
        self.assertEqual([0, 1], [f["entry"] for f in failures])

    def test_fingerprint_changes_with_output_affecting_settings(self):
        first = LLMGenParams(max_tokens=500, temperature=0.1)
        second = LLMGenParams(max_tokens=500, temperature=0.9)
        self.assertNotEqual(tp.three_pass_fingerprint("text", "m", 6000, first),
                            tp.three_pass_fingerprint("text", "m", 6000, second))

    def test_fingerprint_changes_for_structured_output_mode(self):
        first = LLMGenParams(structured_output="auto")
        second = LLMGenParams(structured_output="off")
        self.assertNotEqual(tp.three_pass_fingerprint("text", "m", 6000, first),
                            tp.three_pass_fingerprint("text", "m", 6000, second))
    def _payloads(self):
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        # Narration is now sent with the batch as context, so pass 2
        # answers for both entries; its answer for the narration line is
        # discarded in favour of the deterministic NARRATOR.
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm."}]
        return seg, named, instructed

    def test_completed_stage_is_not_recomputed_on_resume(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg, named, instructed = self._payloads()
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            crashing = _client_returning([seg])  # only pass-1 payload; pass 2 exhausts retries
            with self.assertRaises(tp.PassExhausted):
                tp.run_three_pass(crashing, "m", source, params, chunk_size=6000,
                                  output_path=out)
            cp = tp.three_pass_checkpoint_path(out)
            self.assertTrue(os.path.exists(cp))
            resume_client = _client_returning([named, instructed])
            entries = tp.run_three_pass(resume_client, "m", source, params,
                                        chunk_size=6000, output_path=out)
            self.assertEqual(2, len(entries))
            self.assertEqual("ELENA", entries[1]["speaker"])

    def test_resume_preserves_resolutions_and_accumulates_pass_elapsed(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg, named, instructed = self._payloads()
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            fingerprint = tp.three_pass_fingerprint(source, "m", 6000, params)
            tp._save_three_pass_checkpoint(
                out, fingerprint, "segment", seg, 1, [], [],
                resolutions=["context_rescue:2000"],
                elapsed_s={"segment": 12.5, "attribute": 3.25})
            tp.run_three_pass(_client_returning([named, instructed]), "m", source,
                              params, chunk_size=6000, output_path=out)
            with open(tp.three_pass_manifest_path(out)) as fh:
                manifest = json.load(fh)
        self.assertEqual("context_rescue:2000", manifest["chunks"][0]["resolution"])
        self.assertGreaterEqual(manifest["passes"]["segment"]["elapsed_s"], 12.5)
        self.assertGreaterEqual(manifest["passes"]["attribute"]["elapsed_s"], 3.25)
        self.assertFalse(manifest["legacy_resume"])

    def test_failed_attribution_attempt_is_checkpointed_for_next_resume(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg, _, _ = self._payloads()
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        bad = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
               {"n": 1, "head": "Tell me the", "speaker": "NARRATOR"}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            fingerprint = tp.three_pass_fingerprint(source, "m", 6000, params)
            tp._save_three_pass_checkpoint(
                out, fingerprint, "segment", seg, 1, [], [],
                resolutions=["clean"], elapsed_s={"segment": 2.0, "attribute": 4.0})
            with self.assertRaises(tp.PassExhausted):
                tp.run_three_pass(_client_returning([bad] * 4), "m", source,
                                  params, chunk_size=6000, output_path=out)
            with open(tp.three_pass_checkpoint_path(out)) as fh:
                checkpoint = json.load(fh)
        self.assertEqual("attribute_failed", checkpoint["stage"])
        self.assertGreaterEqual(checkpoint["elapsed_s"]["attribute"], 4.0)


if __name__ == "__main__":
    unittest.main()


class FreezeEnforcementTests(unittest.TestCase):
    def test_attribute_text_comes_byte_exact_from_frozen(self):
        # The model no longer returns text at all - only {n, head, speaker} - so
        # the output text is always the frozen text verbatim, and nothing the
        # model does to a body can corrupt it.
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        resp = [{"n": 0, "head": "Tell me the", "speaker": "ELENA"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([resp]), "m", frozen, p, roster=[])
        self.assertEqual("Tell me the truth.", out[0]["text"])
        self.assertEqual("ELENA", out[0]["speaker"])

    def test_attribute_survives_gemma_style_body_drift(self):
        # Reproduction of the failure that crashed the real run: a weak model that
        # would have mangled a long line's body. Under the index+head contract it
        # only echoes the head + speaker, so the batch validates and the frozen
        # text is preserved byte-exact - no more "entry N text changed" abort.
        frozen = [{"type": "NARRATOR",
                   "text": "The strength in those clinging fingers was weak, and not "
                           "even Beatrice knew what she was trying to do."},
                  {"type": "SPOKEN", "text": "Thank you―― Goodbye, Betty."}]
        resp = [{"n": 0, "head": "The strength in those", "speaker": "NARRATOR"},
                {"n": 1, "head": "Thank you―― Goodbye,", "speaker": "RYUZU"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=800, temperature=0.1)
        out = tp.attribute_batch(_client_returning([resp]), "m", frozen, p, roster=[])
        self.assertEqual(frozen[0]["text"], out[0]["text"])
        self.assertEqual("RYUZU", out[1]["speaker"])

    def test_attribute_preserves_pause_after_and_drops_type(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([named]), "m", frozen, p, roster=[])
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertNotIn("type", out[0])
        self.assertEqual("NARRATOR", out[0]["speaker"])

    def test_attribute_fallback_preserves_pause_after(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me.", "pause_after": 500}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]  # never names the spoken line
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([bad]), "m", frozen, p, roster=[],
                                 max_retries=1, on_exhaustion="fallback")
        self.assertEqual(500, out[0]["pause_after"])
        self.assertNotIn("type", out[0])

    def test_instruct_preserves_pause_after(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        good = [{"n": 0, "head": "The room was", "instruct": "Cold."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([good]), "m", prior, p)
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertEqual("Cold.", out[0]["instruct"])

    def test_instruct_keeps_speaker_and_text_from_prior(self):
        # Pass 3 returns only {n, head, instruct}; speaker and text come byte-exact
        # from the prior entry, so neither can change.
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "Firm."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([resp]), "m", prior, p)
        self.assertEqual("ELENA", out[0]["speaker"])
        self.assertEqual("Tell me.", out[0]["text"])
        self.assertEqual("Firm.", out[0]["instruct"])
        self.assertEqual("Firm.", out[0]["instruct"])


class SegmentRepairTypeSafetyTests(unittest.TestCase):
    def test_segment_repair_does_not_merge_empty_across_type(self):
        # An empty SPOKEN unit between a NARRATOR and a SPOKEN entry must NOT be
        # converted into a pause on the NARRATOR + dropped before the gate; it
        # should stay so validate_segment_quality's empty_text finding sees it.
        entries = [{"type": "NARRATOR", "text": "He spoke softly."},
                   {"type": "SPOKEN", "text": ""},
                   {"type": "SPOKEN", "text": "Hello."}]
        res = build_deterministic_repair(entries, "He spoke softly. Hello.",
                                         merge_empty_into_pause=False)
        self.assertEqual(3, len(res["entries"]))
        self.assertNotIn("pause_after", res["entries"][0])

    def test_single_pass_default_still_merges_empty_into_pause(self):
        entries = [{"speaker": "NARRATOR", "text": "He spoke softly."},
                   {"speaker": "ELENA", "text": ""}]
        res = build_deterministic_repair(entries, "He spoke softly.")
        self.assertEqual(1, len(res["entries"]))  # empty dropped
        self.assertEqual(1000, res["entries"][0]["pause_after"])


class ContextBleedTests(unittest.TestCase):
    def test_bleed_helper_flags_context_only_entry(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        ctx = "the quiet harbor lay still under the morning fog and gulls"
        entries = [{"type": "NARRATOR", "text": chunk},
                   {"type": "SPOKEN", "text": ctx}]
        self.assertTrue(tp._output_has_context_bleed(entries, chunk, ctx, ""))

    def test_bleed_helper_ignores_short_generic_line(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        entries = [{"type": "NARRATOR", "text": chunk}, {"type": "SPOKEN", "text": "Yes."}]
        self.assertFalse(tp._output_has_context_bleed(entries, chunk, "Yes.", ""))

    def test_bleed_helper_flags_context_appended_inside_target_entry(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        ctx = "the quiet harbor lay perfectly still beneath the pale morning fog"
        entries = [{"type": "NARRATOR", "text": chunk + " " + ctx}]
        self.assertTrue(tp._output_has_context_bleed(entries, chunk, ctx, ""))

    def test_context_rescue_rejects_bleeding_output(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        ctx = "the quiet harbor lay still under the morning fog and gulls"
        # Target-correct segmentation PLUS a leaked context sentence: passes
        # recall/trigram/ratio but must be rejected as context bleed -> [].
        payload = [{"type": "NARRATOR", "text": chunk},
                   {"type": "SPOKEN", "text": ctx}]
        client = _client_returning([payload, payload, payload])
        params = LLMGenParams(system_prompt="s", max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_with_context(client, "m", chunk, ctx, "", params,
                                            max_retries=1)
        self.assertEqual([], out)

    def test_context_bleed_is_not_captured_as_trigram_near_miss(self):
        words = [f"word{i}" for i in range(200)]
        chunk = " ".join(words)
        swapped = words[:]
        for i in range(0, len(swapped) - 1, 25):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        ctx = "the quiet harbor lay perfectly still beneath the pale morning fog"
        payload = [{"type": "NARRATOR", "text": " ".join(swapped)},
                   {"type": "NARRATOR", "text": ctx}]
        sink = []
        out = tp.segment_chunk_with_context(
            _client_returning([payload]), "m", chunk, ctx, "",
            LLMGenParams(max_tokens=500, temperature=0.1), max_retries=0,
            near_miss_sink=sink)
        self.assertEqual([], out)
        self.assertEqual([], sink)


class BoundedContextJoinTests(unittest.TestCase):
    def test_tail_and_head_join_match_naive_slice(self):
        chunks = [f"chunk{i}_" * 50 for i in range(20)]  # ~300 chars each
        index = 12
        for window in tp._CONTEXT_RESCUE_WINDOWS:
            naive_before = "".join(chunks[:index])[-window:]
            naive_after = "".join(chunks[index + 1:])[:window]
            bounded_before = tp._tail_join(chunks[:index], max(tp._CONTEXT_RESCUE_WINDOWS))[-window:]
            bounded_after = tp._head_join(chunks[index + 1:], max(tp._CONTEXT_RESCUE_WINDOWS))[:window]
            self.assertEqual(naive_before, bounded_before)
            self.assertEqual(naive_after, bounded_after)

    def test_join_bounded_does_not_materialize_whole_book(self):
        chunks = ["x" * 1000 for _ in range(100)]  # 100k-char "book"
        joined = tp._tail_join(chunks[:50], max(tp._CONTEXT_RESCUE_WINDOWS))
        # Only enough trailing chunks to cover the max window, not all 50.
        self.assertLess(len(joined), 50 * 1000)
        self.assertGreaterEqual(len(joined), max(tp._CONTEXT_RESCUE_WINDOWS))


class RescueBudgetTests(unittest.TestCase):
    def test_window_fits_returns_true_when_context_unknown(self):
        p = LLMGenParams(max_tokens=500, temperature=0.1)  # context_length None
        self.assertTrue(tp._rescue_prompt_fits("x" * 100000, "y" * 6000, "", 500, p))

    def test_custom_windows_control_rescue_attempts(self):
        # finding #12: windows are configurable. A single small window means at
        # most one segmentation attempt (here it fails -> one call, then []).
        attempts = {"n": 0}

        def create(**_kwargs):
            attempts["n"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        p = LLMGenParams(max_tokens=500, temperature=0.1)  # no context_length cap
        out = tp.rescue_chunk_with_context(client, "m", ["small chunk"], 0, p,
                                           windows=(100,), max_retries=0)
        self.assertEqual([], out)
        self.assertEqual(1, attempts["n"], "one window x (max_retries=0 -> 1 attempt)")

    def test_oversized_window_is_skipped_and_no_call_made(self):
        big = "c " * 15000  # ~30k chars -> prompt+output tokens >> 8192
        calls = {"n": 0}

        def create(**_kwargs):
            calls["n"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        p = LLMGenParams(max_tokens=4000, temperature=0.1, context_length=8192)
        out = tp.rescue_chunk_with_context(client, "m", [big], 0, p)
        self.assertEqual([], out)
        self.assertEqual(0, calls["n"], "no LLM call should be made for over-budget windows")


class ManifestTests(unittest.TestCase):
    def test_dialogue_map_failure_makes_manifest_incomplete(self):
        source = "The room was cold."
        segmented = [{"type": "NARRATOR", "text": source}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold."}]
        client = _client_returning([segmented, instructed])
        with tempfile.TemporaryDirectory() as root, \
             patch.object(tp, "apply_dialogue_map",
                          side_effect=ValueError("bad mapping")):
            out = os.path.join(root, "book.json")
            tp.run_three_pass(client, "m", source,
                              LLMGenParams(max_tokens=500, temperature=0.1),
                              chunk_size=6000, output_path=out)
            with open(tp.three_pass_manifest_path(out), encoding="utf-8") as handle:
                manifest = json.load(handle)
        self.assertEqual("incomplete", manifest["status"])
        self.assertEqual("dialogue_map",
                         manifest["diagnostic_failures"][-1]["pass"])

    def test_manifest_records_clean_resolution_counts_and_timing(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        # Narration is now sent with the batch as context, so pass 2
        # answers for both entries; its answer for the narration line is
        # discarded in favour of the deterministic NARRATOR.
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            tp.run_three_pass(client, "m", source, params, chunk_size=6000, output_path=out)
            man = json.load(open(tp.three_pass_manifest_path(out)))
        self.assertEqual("complete", man["status"])
        self.assertEqual(3, man["progress"]["llm_calls"])
        self.assertEqual("clean", man["chunks"][0]["resolution"])
        self.assertEqual(0, man["counts"]["context_rescued"])
        self.assertEqual(0, man["counts"]["near_miss_accepted"])
        for pass_name in ("segment", "attribute", "instruct"):
            self.assertIn("elapsed_s", man["passes"][pass_name])
            self.assertEqual("complete", man["passes"][pass_name]["status"])

    def test_manifest_written_on_failure_with_failing_chunk(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            crashing = _client_returning([seg])  # pass 2 exhausts -> failure
            with self.assertRaises(tp.PassExhausted):
                tp.run_three_pass(crashing, "m", source, params=LLMGenParams(
                    max_tokens=500, temperature=0.1), chunk_size=6000, output_path=out)
            man = json.load(open(tp.three_pass_manifest_path(out)))
        self.assertEqual("failed", man["status"])
        self.assertEqual("attribute", man["failed_pass"])

    def test_fallback_refuses_output_when_llm_is_unavailable(self):
        source = 'The room was cold. "Tell me the truth."'

        def unavailable(**_kwargs):
            raise ConnectionError("offline")

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=unavailable)))
        params = LLMGenParams(max_tokens=500, temperature=0.1,
                              segmentation="auto")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            with self.assertRaisesRegex(RuntimeError, "LLM unavailable"):
                tp.run_three_pass(
                    client, "m", source, params, chunk_size=6000,
                    output_path=out, on_exhaustion="fallback")
            man = json.load(open(tp.three_pass_manifest_path(out)))

        self.assertEqual("failed", man["status"])
        self.assertEqual("attribute", man["failed_pass"])
        self.assertGreater(man["progress"]["llm_calls"], 0)
        self.assertEqual(
            man["progress"]["llm_calls"],
            man["progress"]["failure_codes"]["api_error"])

    def test_resolution_sink_records_near_miss(self):
        words = [f"word{i}" for i in range(100)]
        source = " ".join(words)
        self.assertEqual([], tp.split_failed_chunk(source))
        swapped = list(words)
        i = 0
        while i + 1 < len(swapped):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            i += 25
        near = [{"type": "NARRATOR", "text": " ".join(swapped)}]
        client = _client_returning([near] * 7)
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        sink = []
        out = tp.segment_chunk_adaptively(client, "m", source, params, resolution_sink=sink)
        self.assertTrue(out)
        self.assertEqual(["near_miss"], sink)


class RecombinationAcceptanceTests(unittest.TestCase):
    def _splittable_source(self, n=400):
        words = [f"w{i}" for i in range(n)]
        return words, ". ".join(" ".join(words[i:i+8]) for i in range(0, n, 8)) + "."

    def test_accepts_recombination_when_trigram_only_defect_at_seam(self):
        # Full chunk fails hard (no near-miss) -> splits. Each half echoes all its
        # words but lightly reordered -> each half is recall-1.0/trigram-reduced
        # (passes its own near-miss gate), and the recombined whole is
        # recall-1.0 with trigram below the gate: a trigram-only seam defect the
        # fix must accept instead of discarding both good halves.
        words, source = self._splittable_source(400)
        self.assertTrue(tp.split_failed_chunk(source))  # confirm it splits

        def create(**kwargs):
            cw = re.findall(r"w\d+", kwargs["messages"][-1]["content"])
            if len(cw) > 300:                      # full chunk -> truncate hard
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:5])}]
            else:                                   # split half -> all words, reordered
                sw = list(cw); i = 0
                while i + 1 < len(sw):
                    sw[i], sw[i+1] = sw[i+1], sw[i]; i += 25
                payload = [{"type": "NARRATOR", "text": " ".join(sw)}]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload)),
                finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertTrue(out, "recombination with trigram-only seam defect must be accepted")
        q = tp.validate_segment_quality(source, out)
        self.assertGreaterEqual(q["metrics"]["source_token_recall"], 0.9)  # content all there

    def test_recombination_floor_predicate_rejects_below_floor(self):
        # The recombination branch now gates on is_trigram_only_near_miss, which
        # enforces the 0.82 floor - a trigram-only defect BELOW the floor (heavy
        # reorder / real loss) must NOT be waived, only one within [0.82, 0.90).
        below = {"passed": False,
                 "findings": [{"code": "low_ordered_trigram_recall"}],
                 "metrics": {"ordered_trigram_recall": 0.60}}
        within = {"passed": False,
                  "findings": [{"code": "low_ordered_trigram_recall"}],
                  "metrics": {"ordered_trigram_recall": 0.85}}
        self.assertFalse(tp.is_trigram_only_near_miss(below))
        self.assertTrue(tp.is_trigram_only_near_miss(within))

    def test_rejects_recombination_on_real_recall_loss(self):
        # Each half drops ~60% of its words -> combined recall low = real content
        # loss (not a seam artifact) -> must NOT be accepted.
        words, source = self._splittable_source(400)

        def create(**kwargs):
            cw = re.findall(r"w\d+", kwargs["messages"][-1]["content"])
            if len(cw) > 300:
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:5])}]
            else:
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:max(3, len(cw)*4//10)])}]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload)),
                finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertEqual([], out)  # real recall loss is not waived by the fix


class HardMaxTokensFollowsConfigTests(unittest.TestCase):
    """The escalation ceiling used to sit at the dataclass default (16384)
    whatever Setup said: a hosted reasoning model that thought for 16k tokens
    was cut off with "cannot grow beyond 16384" while the user had configured
    65536 (2026-09-17, OpenRouter via a GPT-Load gateway)."""

    def test_a_larger_configured_budget_raises_the_ceiling(self):
        self.assertEqual(65536, tp.resolve_hard_max_tokens(65536))

    def test_a_smaller_budget_keeps_the_default_headroom(self):
        self.assertEqual(LLMGenParams.hard_max_tokens, tp.resolve_hard_max_tokens(4096))
        self.assertEqual(16384, LLMGenParams.hard_max_tokens)


class ReasoningTokensFromTraceTests(unittest.TestCase):
    """llama.cpp returns message.reasoning_content but no usage reasoning_tokens.
    The allowance then never grew, and Re:Zero vol. 3 failed pass 1 with
    'cannot grow beyond 512' under Muse reasoning low (2026-09-15)."""

    def test_the_attempt_record_estimates_reasoning_from_the_trace(self):
        from generate_script import call_llm_for_entries
        seen = []

        def create(**_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps([{"n": 0, "speaker": "A"}]),
                                        reasoning_content="x" * 4000),
                finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1100))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        call_llm_for_entries(client, "m", "sys", "user", LLMGenParams(max_tokens=64),
                             log_name="test.log", label="ATTRIBUTE",
                             validate_entries=lambda e: {"passed": True},
                             attempt_observer=seen.append)
        self.assertEqual(1000, seen[0]["reasoning_tokens"])

    def test_a_server_that_counts_reasoning_is_believed(self):
        from generate_script import call_llm_for_entries
        seen = []

        def create(**_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps([{"n": 0, "speaker": "A"}]),
                                        reasoning_content="x" * 4000),
                finish_reason="stop")], usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=1100,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=777)))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        call_llm_for_entries(client, "m", "sys", "user", LLMGenParams(max_tokens=64),
                             log_name="test.log", label="ATTRIBUTE",
                             validate_entries=lambda e: {"passed": True},
                             attempt_observer=seen.append)
        self.assertEqual(777, seen[0]["reasoning_tokens"])



class AttributionContextKnobTests(unittest.TestCase):
    """The pass-2 knobs (window size, surrounding text) and the pass-1 output
    ceiling refusal, each defaulting to the behaviour every stored score was
    measured with."""

    SEG = [{"type": "NARRATOR", "text": "Earlier that day the party had argued."},
           {"type": "SPOKEN", "text": "We should go back."},
           {"type": "NARRATOR", "text": "Ranta slurped his soup."},
           {"type": "SPOKEN", "text": "Tell us already."},
           {"type": "SPOKEN", "text": "Don't underestimate me!"},
           {"type": "NARRATOR", "text": "The fire burned low."},
           {"type": "SPOKEN", "text": "Good night."}]

    def test_surround_requotes_spoken_and_respects_the_budget(self):
        sur = tp.build_window_surround(self.SEG, [2, 3, 4], 200)
        self.assertEqual('Earlier that day the party had argued. “We should go back.”',
                         sur["before"])
        self.assertEqual('The fire burned low. “Good night.”', sur["after"])
        tight = tp.build_window_surround(self.SEG, [2, 3, 4], 25)
        self.assertEqual('“We should go back.”', tight["before"])   # nearest first
        self.assertEqual("The fire burned low.", tight["after"])
        self.assertEqual({"before": "", "after": ""}, tp.build_window_surround(self.SEG, [2, 3], 0))
        self.assertEqual("", tp.build_window_surround(self.SEG, [0, 1], 500)["before"])

    def test_attribute_request_is_unchanged_without_surround_and_wrapped_with_it(self):
        params = LLMGenParams(structured_output="off")
        batch = [{"type": "SPOKEN", "text": "Tell us already."}]
        _, plain = tp.build_attribute_request(batch, params, ["RANTA"])
        _, same = tp.build_attribute_request(batch, params, ["RANTA"],
                                             surround={"before": "", "after": ""})
        self.assertEqual(plain, same)
        _, wrapped = tp.build_attribute_request(
            batch, params, ["RANTA"], surround={"before": "B-text", "after": "A-text"})
        self.assertTrue(wrapped.startswith(tp.SURROUND_BEFORE_HEADER + "\nB-text\n\n"))
        self.assertTrue(wrapped.endswith("\n\n" + tp.SURROUND_AFTER_HEADER + "\nA-text"))
        self.assertIn(plain, wrapped)          # the measured body is untouched inside

    def test_window_size_is_honoured(self):
        entries = [{"type": "SPOKEN", "text": f"line {i}"} for i in range(23)]
        self.assertEqual([10, 10, 3], [len(b) for b in tp.iter_unique_entry_batches(entries, 10)])
        self.assertEqual([23], [len(b) for b in tp.iter_unique_entry_batches(entries)])

    def test_fingerprint_keeps_its_identity_at_the_defaults(self):
        params = LLMGenParams()
        base = tp.three_pass_fingerprint("text", "m", 3000, params)
        self.assertEqual(base, tp.three_pass_fingerprint(
            "text", "m", 3000, params, attribute_batch_size=25, attribute_context_chars=0))
        self.assertNotEqual(base, tp.three_pass_fingerprint(
            "text", "m", 3000, params, attribute_batch_size=10))
        self.assertNotEqual(base, tp.three_pass_fingerprint(
            "text", "m", 3000, params, attribute_context_chars=1500))

    def test_preflight_flags_a_chunk_the_output_ceiling_cannot_fit(self):
        text = " ".join(["word"] * 12000)      # one 12k-word chunk at 30000 chars is ~60k chars; split
        settings = {"chunk_size": 30000, "max_tokens": 4096,
                    "segment_output_ratio": 3.0, "segmentation": "auto"}
        report = tp.build_three_pass_request_preflight(text, settings, 0, 1)
        self.assertEqual(16384, report["output_ceiling"])
        self.assertTrue(report["exceeds_output_ceiling"])
        self.assertLess(report["suggested_chunk_size"], 30000)
        self.assertGreaterEqual(report["suggested_chunk_size"], 500)
        small = tp.build_three_pass_request_preflight(text, dict(settings, chunk_size=3000), 0, 1)
        self.assertFalse(small["exceeds_output_ceiling"])
        self.assertIsNone(small["suggested_chunk_size"])

    def test_preflight_counts_the_surround_in_the_attribute_estimate(self):
        text = 'Narration. "Spoken words." More narration.'
        settings = {"chunk_size": 6000, "max_tokens": 4096,
                    "segment_output_ratio": 3.0, "segmentation": "auto"}
        plain = tp.build_three_pass_request_preflight(text, settings, 32768, 1)
        wide = tp.build_three_pass_request_preflight(
            text, dict(settings, attribute_context_chars=3000), 32768, 1)
        attr = lambda r: max(q["prompt_tokens"] for q in r["requests"] if q["stage"] == "attribute")
        self.assertGreater(attr(wide), attr(plain) + 1500)

    def test_prompt_variant_and_context_reach_the_attribution_request(self):
        """The product path asks the attribution question the selected way
        (here michel2, whose system prompt describes the marked passage) and
        wraps it in the surrounding text when the knob is on."""
        source = ('Morning came. The room was cold. "Tell me the truth." '
                  'She waited. "I cannot." Night fell over the house.')
        seg = [{"type": "NARRATOR", "text": "Morning came."},
               {"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."},
               {"type": "NARRATOR", "text": "She waited."},
               {"type": "SPOKEN", "text": "I cannot."},
               {"type": "NARRATOR", "text": "Night fell over the house."}]
        seen = []

        def create(**kwargs):
            messages = kwargs["messages"]
            seen.append((messages[0]["content"], messages[-1]["content"]))
            if len(seen) == 1:
                content = json.dumps(seg)
            elif "PASSAGE" in messages[-1]["content"]:
                # pass 2, michel2: one object per marked entry of the window,
                # read off the markers (|n|"..."|n| spoken, [n] narration)
                body = messages[-1]["content"].split("PASSAGE:", 1)[1].split("AFTER THE PASSAGE")[0]
                spoken = {int(m.group(1)) for m in re.finditer(r'\|(\d+)\|"', body)}
                narr = {int(m.group(1)) for m in re.finditer(r'(?m)^\[(\d+)\] ', body)}
                content = json.dumps([{"n": i, "speaker": "ELENA" if i in spoken else "NARRATOR"}
                                      for i in sorted(spoken | narr)])
            else:
                body = messages[-1]["content"]
                content = json.dumps([{"n": int(m.group(1)), "head": " ".join(m.group(2).split()[:3]),
                                       "instruct": "Plain."}
                                      for m in re.finditer(r'"n": (\d+), "speaker": "[^"]*", "text": "([^"]*)"', body)])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=500, temperature=0.1, structured_output="off")
        tp.run_three_pass(client, "m", source, params, chunk_size=6000,
                          attribute_batch_size=2, attribute_context_chars=200,
                          attribute_prompt_variant="michel2")
        attribute_calls = [(s, u) for s, u in seen if "PASSAGE" in u]
        self.assertTrue(attribute_calls, "no michel2 attribution request was made")
        system, user = attribute_calls[0]
        self.assertIn("|n|", system)                       # michel2's own system prompt
        self.assertIn("BEFORE THE PASSAGE", user)          # the surround knob, in the variant's shape
        self.assertIn("Morning came.", user)

    def test_preset_texts_reach_the_run_and_the_fingerprint(self):
        source = 'Morning came. "Tell me the truth." She waited.'
        seg = [{"type": "NARRATOR", "text": "Morning came."},
               {"type": "SPOKEN", "text": "Tell me the truth."},
               {"type": "NARRATOR", "text": "She waited."}]
        seen = []

        def create(**kwargs):
            messages = kwargs["messages"]
            seen.append((messages[0]["content"], messages[-1]["content"]))
            if len(seen) == 1:
                content = json.dumps(seg)
            elif "MY RULE" in messages[0]["content"]:
                content = json.dumps([{"n": 0, "speaker": "NARRATOR"}, {"n": 1, "speaker": "ELENA"},
                                      {"n": 2, "speaker": "NARRATOR"}])
            else:
                body = messages[-1]["content"]
                content = json.dumps([{"n": int(m.group(1)), "head": " ".join(m.group(2).split()[:3]),
                                       "instruct": "Plain."}
                                      for m in re.finditer(r'"n": (\d+), "speaker": "[^"]*", "text": "([^"]*)"', body)])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=500, temperature=0.1, structured_output="off")
        texts = {"system": "MY RULE: answer NARRATOR for narration.",
                 "user": "ESTABLISHED ROSTER: {roster}\n\nName the speaker:\n\n{batch}", "example": ""}
        tp.run_three_pass(client, "m", source, params, chunk_size=6000,
                          attribute_prompt_variant="default", attribute_prompt_texts=texts)
        attribute_calls = [(s, u) for s, u in seen if "Name the speaker" in u]
        self.assertTrue(attribute_calls, "the edited default template was not used")
        self.assertEqual("MY RULE: answer NARRATOR for narration.", attribute_calls[0][0])
        base = tp.three_pass_fingerprint("text", "m", 3000, params)
        self.assertEqual(base, tp.three_pass_fingerprint("text", "m", 3000, params,
                                                         attribute_prompt_texts=None))
        self.assertNotEqual(base, tp.three_pass_fingerprint("text", "m", 3000, params,
                                                            attribute_prompt_texts=texts))
