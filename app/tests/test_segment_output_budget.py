"""Bound ordinary Pass 1's JSON overhead without bypassing request limits."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import generate_script as gs
import three_pass_generate as tp
from compact_pass1 import segment_chunk_compact, split_units
from generate_script import LLMGenParams
from llm_provider import ConfiguredOpenAI, FailoverClient
from lmstudio_settings import TokenBudgetError


class SegmentOutputBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = Path(temporary.name)
        redirect = patch.object(gs, "get_response_log_path",
                                return_value=str(self.logs / "responses.log"))
        redirect.start()
        self.addCleanup(redirect.stop)
        self.source = "\n".join(f"Leaf{i} grows." for i in range(140))
        self.entries = [{"type": "NARRATOR", "text": line}
                        for line in self.source.splitlines()]
        self.params = LLMGenParams(
            max_tokens=4096, hard_max_tokens=16384, temperature=0,
            structured_output="off", segmentation="llm",
            segment_system_prompt="Segment the source.",
            segment_user_prompt_template="{chunk}")

    def get_client(self, create):
        return NS(chat=NS(completions=NS(create=create)))

    def get_reply(self, entries, finish="stop"):
        return NS(choices=[NS(message=NS(content=json.dumps(entries)),
                              finish_reason=finish)], usage=None)

    def call_segment(self, client, params=None, retries=0, attempts=None):
        return tp._call_segment(
            client, "fixture", self.source, "Segment the source.", self.source,
            params or self.params, "SEGMENT", retries, None,
            attempt_observer=None if attempts is None else attempts.append)

    def test_known_line_shape_floor_and_prose_control(self):
        # Literal two-space JSON rows for 140 copies of "a b" occupy 7,142
        # characters: ceil(7142/3)=2381, versus the word-only allowance of840.
        source = "\n".join(["a b"] * 140)
        self.assertEqual(2381, tp.resolve_completion_ceiling(
            280, self.params, source_text=source))
        self.assertEqual(840, tp.resolve_completion_ceiling(
            280, self.params, source_text=" ".join(["a b"] * 140)))
        self.assertEqual(840, tp.resolve_completion_ceiling(280, self.params))
        self.assertEqual(3405, tp.resolve_completion_ceiling(
            280, self.params, 1024, source_text=source))

    def test_unicode_escaping_crlf_and_blank_line_arithmetic(self):
        cases = [("他は言った。", 2521), ('A "quote" \\ path.', 3174)]
        for line, expected in cases:
            with self.subTest(line=line):
                source = "\r\n \t\r\n".join([line] * 140)
                self.assertEqual(expected, tp.resolve_completion_ceiling(
                    len(source.split()), self.params, source_text=source))
        for source in ("", " \t\r\n"):
            self.assertEqual(512, tp.resolve_completion_ceiling(
                0, self.params, source_text=source))

    def test_actual_sdk_receives_structural_budget_and_faithful_reply_passes(self):
        sent = []
        # The controlled provider needs 2858 tokens for this synthetic fixture.
        # This discriminates the old840 cap; it is not model token usage.
        def create(**kwargs):
            sent.append(kwargs["max_tokens"])
            return self.get_reply(self.entries if kwargs["max_tokens"] >= 2858 else [],
                                  "stop" if kwargs["max_tokens"] >= 2858 else "length")
        self.assertEqual(self.entries, self.call_segment(self.get_client(create)))
        self.assertEqual([2858], sent)

    def test_reasoning_is_added_once_to_actual_request(self):
        sent = []
        client = self.get_client(lambda **kw: sent.append(kw["max_tokens"])
                                 or self.get_reply(self.entries))
        params = replace(self.params, reasoning_allowance=1024)
        self.assertEqual(self.entries, self.call_segment(client, params))
        self.assertEqual([3882], sent)

    def test_profile_task_request_and_context_caps_remain_authoritative(self):
        for changes, profile, expected in (
                ({"hard_max_tokens": 700}, {}, 700),
                ({}, {"max_output_tokens": 600}, 600),
                ({"request_output_ceiling": 500}, {}, 500),
                ({"context_length": 1600}, {}, 418),
                ({"hard_max_tokens": 700, "request_output_ceiling": 500},
                 {"max_output_tokens": 600}, 500)):
            with self.subTest(changes=changes, profile=profile):
                sent = []
                raw = self.get_client(lambda **kw: sent.append(kw["max_tokens"])
                                      or self.get_reply([], "length"))
                client = ConfiguredOpenAI(raw, {}, limits=profile) if profile else raw
                self.assertEqual([], self.call_segment(client, replace(self.params, **changes)))
                self.assertEqual([expected], sent)

    def test_input_refusal_happens_before_sdk_dispatch(self):
        create = Mock()
        attempts = []
        self.assertEqual([], self.call_segment(
            self.get_client(create), replace(self.params, max_input_tokens=10),
            attempts=attempts))
        create.assert_not_called()
        self.assertEqual("context_budget", attempts[0]["error_category"])

    def test_blank_and_explicit_secondary_limits_keep_chunk_bound(self):
        client = Mock(spec=FailoverClient)
        params = replace(self.params, task_max_tokens=4096, task_hard_max_tokens=16384)
        original = copy.deepcopy(params)
        seen = []
        def dispatch(c, model, system, user, bounded, **kwargs):
            resolved = gs.ensure_run_request_params(c, bounded)
            seen.append((resolved.max_tokens, resolved.hard_max_tokens))
            return self.entries
        with patch.object(tp, "call_llm_for_entries", side_effect=dispatch):
            for profile in ({}, {"max_output_tokens": 8000}, {"max_output_tokens": 300}):
                client.ensure_active_runtime_profile.return_value = {
                    "config": profile, "context_length": 16384}
                self.call_segment(client, params)
        self.assertEqual([(2858, 2858), (2858, 2858), (300, 300)], seen)
        self.assertEqual(original, params)

    def test_incomplete_and_duplicated_source_still_fail_fidelity(self):
        for entries in (self.entries[:10], self.entries + self.entries):
            with self.subTest(length=len(entries)):
                client = self.get_client(lambda **kw: self.get_reply(entries))
                self.assertEqual([], self.call_segment(client))

    def test_length_finish_retries_grow_only_to_finite_structural_ceiling(self):
        sent = []
        client = self.get_client(lambda **kw: sent.append(kw["max_tokens"])
                                 or self.get_reply([], "length"))
        attempts = []
        self.assertEqual([], self.call_segment(
            client, replace(self.params, max_tokens=1000), retries=2, attempts=attempts))
        self.assertEqual([1000, 1500, 2250], sent)
        self.assertTrue(all(a["finish_reason"] == "length" for a in attempts))
        self.assertLessEqual(max(sent), 2858)

    def test_preflight_and_execution_share_visible_estimate_with_reasoning_once(self):
        settings = {"chunk_size": 6000, "max_tokens": 4096,
                    "segment_output_ratio": 3.0, "segmentation": "llm"}
        for allowance in (0, 1024):
            with self.subTest(allowance=allowance):
                params = replace(self.params, reasoning_allowance=allowance)
                report = tp.build_three_pass_request_preflight(
                    self.source, settings, 0, 1, context_windows=[1], params=params)
                self.assertEqual(2858, report["largest_predicted_completion"])
                requests = [r for r in report["requests"]
                            if r["stage"] in ("segment", "segment_context_rescue")]
                self.assertTrue(requests)
                self.assertTrue(all(r["predicted_completion_tokens"] == 2858 + allowance
                                    for r in requests))
        report = tp.build_three_pass_request_preflight(
            self.source, settings, 0, 1,
            params=replace(self.params, hard_max_tokens=1000))
        self.assertTrue(report["exceeds_output_ceiling"])

    def test_length_exhaustion_still_uses_bounded_adaptive_split(self):
        sent = []
        def create(**kwargs):
            source = kwargs["messages"][-1]["content"].split(
                "\n\nYour previous response was rejected", 1)[0]
            sent.append((len(source), kwargs["max_tokens"]))
            if len(source) > 1200:
                return self.get_reply([], "length")
            return self.get_reply([{"type": "NARRATOR", "text": line}
                                   for line in source.splitlines()])
        resolution = []
        result = tp.segment_chunk_adaptively(
            self.get_client(create), "fixture", self.source, self.params,
            resolution_sink=resolution)
        self.assertEqual(self.entries, result)
        self.assertEqual(["adaptive_split"], resolution)
        self.assertGreaterEqual(len(sent), 3)
        self.assertLessEqual(len(sent), 7)
        self.assertTrue(all(budget <= 2858 for _, budget in sent))

    def test_context_rescue_uses_target_bound_and_refuses_bleed_or_full_context(self):
        context = "Earlier context contains additional unrelated information outside this source."
        sent = []
        def create(**kwargs):
            sent.append(kwargs["max_tokens"])
            return self.get_reply(self.entries)
        params = replace(self.params, context_length=16384, reasoning_allowance=1024)
        self.assertEqual(self.entries, tp.segment_chunk_with_context(
            self.get_client(create), "fixture", self.source, context, context,
            params, max_retries=0))
        self.assertEqual([3882], sent)
        create = Mock()
        self.assertEqual([], tp.segment_chunk_with_context(
            self.get_client(create), "fixture", self.source, context * 100, context,
            replace(params, context_length=1600), max_retries=0))
        create.assert_not_called()
        leaking = self.get_client(lambda **kw: self.get_reply(
            self.entries + [{"type": "NARRATOR", "text": context}]))
        self.assertEqual([], tp.segment_chunk_with_context(
            leaking, "fixture", self.source, context, "", params, max_retries=0))

    def test_large_target_context_admission_uses_source_aware_estimate(self):
        params = replace(self.params, context_length=16384)
        with patch.object(tp, "resolve_completion_ceiling",
                          wraps=tp.resolve_completion_ceiling) as ceiling:
            records = tp.get_segment_chunk_records(self.source, 50000, params)
        self.assertEqual([self.source], [r["text"] for r in records])
        self.assertTrue(all(c.kwargs["source_text"] == self.source
                            for c in ceiling.call_args_list))
        with self.assertRaises(TokenBudgetError):
            tp.get_segment_chunk_records(self.source, 50000,
                                         replace(params, context_length=800))

    def test_compact_label_schema_keeps_words_only_cap(self):
        seen = []
        def create(**kwargs):
            seen.append(kwargs["max_tokens"])
            units = split_units(self.source)
            return self.get_reply([{"n": n, "type": "NARRATOR"}
                                   for n in range(1, len(units) + 1)])
        result = segment_chunk_compact(self.get_client(create), "fixture",
                                       self.source, self.params)
        self.assertTrue(result)
        self.assertEqual([840], seen)
