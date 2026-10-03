from tests.test_support import assert_file_lock_released
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import tempfile

import generate_script
import core
import review_script
from lmstudio_settings import (get_effective_max_tokens, get_next_retry_max_tokens,
                               TokenBudgetError)


class LlmReviewTests(unittest.TestCase):
    def test_empty_object_runs_validation_and_does_not_allow_empty_entry_arrays(self):
        def require_description(payload):
            if not payload.get("description"):
                raise ValueError("description is required")
        for object_request in (True, False):
            with self.subTest(object_request=object_request), tempfile.TemporaryDirectory() as tmp:
                attempts = []
                client = self._client_with_responses(["{}" if object_request else "[]"] * 3)
                params = generate_script.LLMGenParams(temperature=0.2)
                with patch.object(generate_script, "get_response_log_path", return_value=str(Path(tmp, "response.log"))):
                    if object_request:
                        result = generate_script.call_llm_for_object(
                            client, "model", "system", "user", params, label="TEST OBJECT",
                            validate_object=require_description, max_retries=2,
                            attempt_observer=attempts.append)
                    else:
                        result = generate_script.call_llm_for_entries(
                            client, "model", "system", "user", params, "response.log", "TEST ARRAY",
                            max_retries=2, attempt_observer=attempts.append)
                self.assertEqual([], result)
                self.assertEqual(3, len(attempts))
                self.assertTrue(all(attempt["outcome"] != "accepted" for attempt in attempts))
                self.assertIn("invalid_object" if object_request else "malformed_json",
                              attempts[-1]["failure_codes"])

    def test_response_log_is_isolated_by_run_id(self):
        with patch.dict(os.environ, {"ALEXANDRIA_RUN_ID": "run_test"}):
            path = generate_script.get_response_log_path("llm_responses.log")
        self.assertTrue(path.endswith("logs/responses/run_test/llm_responses.log"))

    @staticmethod
    def _client_with_responses(contents):
        responses = iter(contents)
        def create(**_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=next(responses)), finish_reason="stop")], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def test_chunk_quality_failure_retries_then_accepts_complete_response(self):
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = [{"speaker": "NARRATOR", "text": "word0 word1", "instruct": "neutral"}]
        complete = [{"speaker": "NARRATOR", "text": source, "instruct": "neutral"}]
        client = self._client_with_responses([json.dumps(incomplete), json.dumps(complete)])
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(
            client, "model", source, 1, 1, params, max_retries=1)

        self.assertEqual(complete, result)

    def test_process_chunk_default_budget_is_five_attempts(self):
        # Regression: default max_retries must stay 4 (5 total attempts), not
        # silently regress to the old 2 (3 attempts). Live reproduction of two
        # real overnight batch failures measured a genuine ~40% single-attempt
        # success rate for the specific failure this budget exists to absorb
        # (the model stopping a few lines into a chunk); 3 attempts recovers
        # ~78% of the time, 5 attempts ~92%.
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([{"speaker": "NARRATOR", "text": "word0", "instruct": "n"}])
        complete = json.dumps([{"speaker": "NARRATOR", "text": source, "instruct": "n"}])
        # Fails 4 times, succeeds on the 5th -- only reachable if the default
        # budget is actually 5 attempts, not 3.
        client = self._client_with_responses(
            [incomplete, incomplete, incomplete, incomplete, complete])
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual(json.loads(complete), result)

    def test_process_chunk_default_budget_stops_after_exactly_five_attempts(self):
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([
            {"speaker": "NARRATOR", "text": "word0", "instruct": "n"}])
        calls = 0

        def create(**_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=incomplete), finish_reason="stop")],
                usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual([], result)
        self.assertEqual(5, calls)

    def test_attempt_observer_receives_token_and_finish_metrics(self):
        source = "one two three four five"
        response = json.dumps([{"speaker": "NARRATOR", "text": source,
                                "instruct": "neutral"}])
        usage = SimpleNamespace(prompt_tokens=12, completion_tokens=8)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response),
                                         finish_reason="stop")], usage=usage))))
        attempts = []

        generate_script.process_chunk(
            client, "model", source, 1, 1,
            generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1),
            attempt_observer=attempts.append)

        self.assertEqual(1, len(attempts))
        self.assertEqual("stop", attempts[0]["finish_reason"])
        self.assertEqual(12, attempts[0]["prompt_tokens"])
        self.assertEqual(8, attempts[0]["completion_tokens"])
        self.assertEqual("accepted", attempts[0]["outcome"])

    def test_attempt_observer_records_quality_rejection_codes(self):
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([
            {"speaker": "NARRATOR", "text": "word0", "instruct": "neutral"}])
        attempts = []

        generate_script.process_chunk(
            self._client_with_responses([incomplete]), "model", source, 1, 1,
            generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1),
            max_retries=0, attempt_observer=attempts.append)

        self.assertEqual("quality_rejected", attempts[0]["outcome"])
        self.assertIn("low_source_token_recall", attempts[0]["failure_codes"])

    def test_api_retry_policy_records_rate_limit_and_honors_profile_limit(self):
        calls = []
        attempts = []

        def rate_limited(**_kwargs):
            calls.append(True)
            error = RuntimeError("too many requests")
            error.status_code = 429
            raise error

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=rate_limited)))
        result = generate_script.call_llm_for_entries(
            client, "model", "system", "user",
            generate_script.LLMGenParams(
                max_tokens=100, temperature=0.0, api_retry_limit=1,
                retry_initial_delay_seconds=0,
                provider_extra_body={"gateway_token": "private"}),
            "llm_responses.log", "TEST", max_retries=4,
            attempt_observer=attempts.append)

        self.assertEqual([], result)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(attempts))
        self.assertEqual("rate_limited", attempts[0]["error_category"])
        self.assertTrue(attempts[0]["retryable"])
        self.assertEqual(0, attempts[0]["next_retry_seconds"])
        self.assertEqual("model", attempts[0]["request"]["model"])
        self.assertEqual("system", attempts[0]["request"]["system_prompt"])
        self.assertEqual("user", attempts[0]["request"]["user_prompt"])
        self.assertNotIn("api_key", attempts[0]["request"])
        self.assertEqual("[REDACTED]", attempts[0]["request"]["extra_body"]["gateway_token"])
        self.assertIsNone(attempts[1]["next_retry_seconds"])

    def test_context_budget_failure_keeps_its_recovery_record(self):
        attempts = []
        result = generate_script.call_llm_for_entries(
            object(), "model", "system", "user",
            generate_script.LLMGenParams(context_length=1),
            "llm_responses.log", "TEST", max_retries=0,
            attempt_observer=attempts.append)

        self.assertEqual([], result)
        self.assertEqual(1, len(attempts))
        self.assertIsNone(attempts[0]["request"]["max_tokens"])
        self.assertEqual("context_budget", attempts[0]["error_category"])
        self.assertFalse(attempts[0]["retryable"])

    def test_chunk_quality_exhaustion_returns_failure_even_with_stop_reason(self):
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([{"speaker": "NARRATOR", "text": "word0", "instruct": "neutral"}])
        client = self._client_with_responses([incomplete, incomplete])
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(
            client, "model", source, 1, 1, params, max_retries=1)

        self.assertEqual([], result)

    def test_early_split_decider_stops_full_chunk_after_second_severe_failure(self):
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([
            {"speaker": "NARRATOR", "text": "word0", "instruct": "neutral"}])
        calls = 0

        def create(**_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=incomplete), finish_reason="stop")],
                usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        result = generate_script.process_chunk(
            client, "model", source, 1, 1,
            generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1),
            allow_early_split=True)

        self.assertEqual([], result)
        self.assertEqual(2, calls)

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

    def test_adjacent_json_arrays_are_combined_in_order(self):
        first = [{"speaker": "NARRATOR", "text": "one", "instruct": "neutral"}]
        second = [{"speaker": "TWO", "text": "two", "instruct": "quiet"}]

        cleaned = generate_script.clean_json_string(
            json.dumps(first) + "\n" + json.dumps(second))

        self.assertEqual(first + second, generate_script.repair_json_array(cleaned))

    def test_adjacent_json_array_overlap_is_rejected(self):
        first = [{"speaker": "NARRATOR", "text": "before the repeated words here",
                  "instruct": "neutral"}]
        second = [{"speaker": "OTHER", "text": "repeated words here after",
                   "instruct": "quiet"}]

        with self.assertRaisesRegex(
                generate_script.AdjacentArrayOverlapError, "repeated words here"):
            generate_script.clean_json_string(
                json.dumps(first) + "\n" + json.dumps(second))

    def test_adjacent_json_array_overlap_retries_with_specific_feedback(self):
        prompts = []
        first = [{"speaker": "NARRATOR", "text": "before repeated words here",
                  "instruct": "neutral"}]
        overlap = [{"speaker": "OTHER", "text": "repeated words here after",
                    "instruct": "quiet"}]
        complete = [{"speaker": "NARRATOR", "text": "complete", "instruct": "neutral"}]
        responses = iter([json.dumps(first) + "\n" + json.dumps(overlap),
                          json.dumps(complete)])

        def create(**kwargs):
            prompts.append(kwargs["messages"][1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=next(responses)), finish_reason="stop")],
                usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        attempts = []
        result = generate_script.call_llm_for_entries(
            client, "model", "system", "text", generate_script.LLMGenParams(),
            "test_responses.log", "TEST", max_retries=1,
            attempt_observer=attempts.append)

        self.assertEqual(complete, result)
        self.assertNotIn("adjacent_array_overlap", prompts[0])
        self.assertIn("adjacent_array_overlap", prompts[1])
        self.assertEqual(["adjacent_array_overlap"], attempts[0]["failure_codes"])
        self.assertEqual("accepted", attempts[1]["outcome"])

    def test_two_word_adjacent_array_boundary_is_not_treated_as_overlap(self):
        first = [{"speaker": "NARRATOR", "text": "before repeated words",
                  "instruct": "neutral"}]
        second = [{"speaker": "OTHER", "text": "repeated words after",
                   "instruct": "quiet"}]

        cleaned = generate_script.clean_json_string(
            json.dumps(first) + "\n" + json.dumps(second))

        self.assertEqual(first + second, generate_script.repair_json_array(cleaned))

    def test_text_between_json_arrays_is_not_silently_discarded(self):
        first = [{"speaker": "NARRATOR", "text": "one", "instruct": "neutral"}]
        second = [{"speaker": "TWO", "text": "two", "instruct": "quiet"}]

        cleaned = generate_script.clean_json_string(
            json.dumps(first) + " malformed " + json.dumps(second))

        self.assertIsNone(generate_script.repair_json_array(cleaned))

    def test_exhausted_ambiguous_arrays_use_quality_gated_raw_salvage(self):
        source = "one two three four five six"
        first = [{"speaker": "NARRATOR", "text": "one two three",
                  "instruct": "neutral"}]
        second = [{"speaker": "NARRATOR", "text": "four five six",
                   "instruct": "neutral"}]
        response = json.dumps(first) + "\nmodel commentary\n" + json.dumps(second)
        attempts = []

        result = generate_script.process_chunk(
            self._client_with_responses([response]), "model", source, 1, 1,
            generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1),
            max_retries=0, attempt_observer=attempts.append)

        self.assertEqual(source, " ".join(entry["text"] for entry in result))
        self.assertEqual("accepted", attempts[0]["outcome"])
        self.assertEqual(["missing_json_array"], attempts[0]["recovery_codes"])
        self.assertNotIn("failure_codes", attempts[0])

    def test_exhausted_incomplete_raw_salvage_still_fails_quality_gate(self):
        source = "one two three four five six seven eight nine ten"
        incomplete = [{"speaker": "NARRATOR", "text": "one two",
                       "instruct": "neutral"}]
        response = json.dumps(incomplete) + "\nmodel commentary\n[{broken}]"
        attempts = []

        result = generate_script.process_chunk(
            self._client_with_responses([response]), "model", source, 1, 1,
            generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1),
            max_retries=0, attempt_observer=attempts.append)

        self.assertEqual([], result)
        self.assertEqual("quality_rejected", attempts[0]["outcome"])
        self.assertIn("low_source_token_recall", attempts[0]["failure_codes"])

    def test_bracketed_trailing_prose_does_not_discard_valid_array(self):
        entries = [{"speaker": "NARRATOR", "text": "one", "instruct": "neutral"}]

        cleaned = generate_script.clean_json_string(
            json.dumps(entries) + "\nNote: preserve [speaker] labels.")

        self.assertEqual(entries, generate_script.repair_json_array(cleaned))

    def test_malformed_trailing_entry_array_is_rejected(self):
        entries = [{"speaker": "NARRATOR", "text": "one", "instruct": "neutral"}]

        cleaned = generate_script.clean_json_string(
            json.dumps(entries) + '\n[{"speaker":}]')

        self.assertIsNone(cleaned)

    def test_retry_budget_only_increases_for_incomplete_output(self):
        self.assertEqual(6144, get_next_retry_max_tokens(
            4096, "token_truncated", 16384))
        self.assertEqual(9216, get_next_retry_max_tokens(
            6144, "incomplete_output", 16384))
        self.assertEqual(4096, get_next_retry_max_tokens(
            4096, "quality_failure", 16384))
        self.assertEqual(7000, get_next_retry_max_tokens(
            6144, "token_truncated", 7000))

    def test_length_retries_progressively_increase_api_budget(self):
        calls = []
        prompts = []
        valid = json.dumps([
            {"speaker": "NARRATOR", "text": "done", "instruct": "neutral"}
        ])
        contents = iter([
            (valid, "length"),
            (valid, "length"),
            (valid, "stop"),
        ])

        def create(**kwargs):
            calls.append(kwargs["max_tokens"])
            prompts.append(kwargs["messages"])
            content, reason = next(contents)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=reason)], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams(max_tokens=4096, hard_max_tokens=16384)

        result = generate_script.call_llm_for_entries(
            client, "model", "system", "text", params,
            "test_responses.log", "TEST", max_retries=2)

        self.assertEqual("done", result[0]["text"])
        self.assertEqual([4096, 6144, 9216], calls)
        self.assertTrue(all(messages[1]["content"] == "text" for messages in prompts))

    def test_incomplete_stop_does_not_increase_unspent_token_budget(self):
        calls = []
        incomplete = json.dumps([{"speaker": "NARRATOR", "text": "one",
                                  "instruct": "neutral"}])
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=400)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (calls.append(kwargs["max_tokens"]) or SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=incomplete),
                                         finish_reason="stop")], usage=usage)))))
        quality = lambda _entries: {"passed": False,
            "metrics": {"output_source_ratio": 0.1},
            "findings": [{"code": "low_source_token_recall", "value": 0.1}]}

        result = generate_script.call_llm_for_entries(
            client, "model", "system", "text", generate_script.LLMGenParams(max_tokens=4096),
            "test_responses.log", "TEST", max_retries=1, validate_entries=quality)

        self.assertEqual([], result)
        self.assertEqual([4096, 4096], calls)

    def test_retry_feedback_for_truncation_cluster_is_plain_english(self):
        quality = {"metrics": {"source_token_recall": 0.53},
                   "findings": [{"code": "low_source_token_recall", "value": 0.53},
                               {"code": "low_ordered_trigram_recall", "value": 0.51},
                               {"code": "output_source_ratio", "value": 0.53}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("about 53%", message)
        self.assertIn("stopping early", message)
        self.assertNotIn("low_source_token_recall", message)
        self.assertNotIn("{", message)

    def test_retry_feedback_for_truncation_cluster_without_recall_metric(self):
        quality = {"metrics": {}, "findings": [{"code": "output_source_ratio"}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("too little", message)
        self.assertIn("stopping early", message)

    def test_retry_feedback_for_other_codes_uses_finding_messages(self):
        quality = {"metrics": {}, "findings": [
            {"code": "missing_fields", "message": "Entry is missing required fields."},
            {"code": "empty_text", "message": "Entry contains no speakable text."}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertEqual(
            "Entry is missing required fields. Entry contains no speakable text.",
            message)

    def test_retry_feedback_falls_back_to_json_when_no_messages_present(self):
        # An unrelated code with no message, to exercise the defensive final
        # fallback (truncation-cluster codes always short-circuit earlier).
        quality = {"metrics": {}, "findings": [{"code": "some_future_code"}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertEqual(json.dumps(quality["findings"], ensure_ascii=False), message)

    def test_incomplete_stop_retry_prompt_uses_plain_english_not_raw_codes(self):
        prompts = []
        incomplete = json.dumps([{"speaker": "NARRATOR", "text": "one",
                                  "instruct": "neutral"}])
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=400)

        def create(**kwargs):
            prompts.append(kwargs["messages"][-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=incomplete), finish_reason="stop")],
                usage=usage)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        quality = lambda _entries: {"passed": False,
            "metrics": {"source_token_recall": 0.3, "output_source_ratio": 0.1},
            "findings": [{"code": "low_source_token_recall", "value": 0.3}]}

        generate_script.call_llm_for_entries(
            client, "model", "system", "text", generate_script.LLMGenParams(max_tokens=4096),
            "test_responses.log", "TEST", max_retries=1, validate_entries=quality)

        self.assertNotIn("low_source_token_recall", prompts[1])
        self.assertIn("stopping early", prompts[1])
        self.assertIn("about 30%", prompts[1])

    def test_is_near_miss_recall_true_above_threshold(self):
        self.assertTrue(generate_script._is_near_miss_recall(
            {"source_token_recall": 0.86}))
        self.assertTrue(generate_script._is_near_miss_recall(
            {"source_token_recall": 0.75}))

    def test_is_near_miss_recall_false_below_threshold_or_missing(self):
        self.assertFalse(generate_script._is_near_miss_recall(
            {"source_token_recall": 0.5}))
        self.assertFalse(generate_script._is_near_miss_recall(
            {"source_token_recall": 0.05}))
        self.assertFalse(generate_script._is_near_miss_recall({}))
        self.assertFalse(generate_script._is_near_miss_recall(None))

    def test_retry_feedback_near_miss_uses_different_wording(self):
        quality = {"metrics": {"source_token_recall": 0.86},
                   "findings": [{"code": "low_source_token_recall", "value": 0.86}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("close but only covered about 86%", message)
        self.assertNotIn("stopping early", message)
        self.assertNotIn("ENTIRE", message)

    def test_retry_feedback_catastrophic_keeps_original_wording(self):
        quality = {"metrics": {"source_token_recall": 0.11},
                   "findings": [{"code": "low_source_token_recall", "value": 0.11}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("stopping early", message)
        self.assertIn("ENTIRE", message)
        self.assertNotIn("close but", message)
        self.assertNotIn("missing content includes", message)

    def test_retry_feedback_catastrophic_quotes_missing_spans(self):
        quality = {"metrics": {"source_token_recall": 0.11},
                   "findings": [{"code": "low_source_token_recall", "value": 0.11}],
                   "missing_source_spans": [
                       {"start_token": 50, "token_count": 40,
                        "preview": "the queen turned to face the shattered gate"}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("stopping early", message)
        self.assertIn('"the queen turned to face the shattered gate ..."', message)
        self.assertIn("about 40 words", message)

    def test_retry_feedback_near_miss_quotes_missing_spans(self):
        quality = {"metrics": {"source_token_recall": 0.86},
                   "findings": [{"code": "low_source_token_recall", "value": 0.86}],
                   "missing_source_spans": [
                       {"start_token": 10, "token_count": 8, "preview": "a short lost aside"},
                       {"start_token": 90, "token_count": 6, "preview": "another dropped phrase"}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("close but", message)
        self.assertIn('"a short lost aside ..."', message)
        self.assertIn('"another dropped phrase ..."', message)

    def test_retry_feedback_unchanged_without_spans_key(self):
        quality = {"metrics": {"source_token_recall": 0.86},
                   "findings": [{"code": "low_source_token_recall", "value": 0.86}]}
        message = generate_script._build_retry_feedback_message(quality)
        self.assertIn("close but only covered about 86%", message)
        self.assertNotIn("missing content includes", message)

    def test_process_chunk_grants_bonus_retry_after_near_miss_final_attempt(self):
        source = " ".join(f"word{index}" for index in range(20))
        # 5 catastrophic-then-near-miss responses to exhaust the default
        # budget (max_retries=4, 5 attempts), then a complete 6th (bonus).
        tiny = json.dumps([{"speaker": "NARRATOR", "text": "word0", "instruct": "n"}])
        near_miss = json.dumps([{"speaker": "NARRATOR", "text": " ".join(
            f"word{index}" for index in range(16)), "instruct": "n"}])
        complete = json.dumps([{"speaker": "NARRATOR", "text": source, "instruct": "n"}])
        client = self._client_with_responses(
            [tiny, tiny, tiny, tiny, near_miss, complete])
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual(json.loads(complete), result)

    def test_process_chunk_bonus_retry_is_exactly_one_attempt(self):
        source = " ".join(f"word{index}" for index in range(20))
        tiny = json.dumps([{"speaker": "NARRATOR", "text": "word0", "instruct": "n"}])
        near_miss = json.dumps([{"speaker": "NARRATOR", "text": " ".join(
            f"word{index}" for index in range(16)), "instruct": "n"}])
        # Final regular attempt (5th) is a near miss; the bonus (6th) call
        # is ALSO a near miss - must not grant a second bonus.
        client = self._client_with_responses(
            [tiny, tiny, tiny, tiny, near_miss, near_miss])
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual([], result)

    def test_process_chunk_no_bonus_retry_for_catastrophic_final_attempt(self):
        # Regression guard: matches
        # test_process_chunk_default_budget_stops_after_exactly_five_attempts's
        # fixture shape exactly - every attempt is catastrophic (~5% recall,
        # not a near miss), so no bonus call should ever fire.
        source = " ".join(f"word{index}" for index in range(20))
        incomplete = json.dumps([
            {"speaker": "NARRATOR", "text": "word0", "instruct": "n"}])
        calls = 0

        def create(**_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=incomplete), finish_reason="stop")],
                usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual([], result)
        self.assertEqual(5, calls)

    def test_near_limit_incomplete_stop_increases_budget(self):
        quality = {"metrics": {"output_source_ratio": 0.2},
                   "findings": [{"code": "output_source_ratio"}]}
        self.assertEqual("increase_tokens", generate_script.get_quality_retry_policy(
            "stop", 950, 1000, quality))

    def test_context_clamped_truncation_does_not_retry(self):
        calls = []
        valid = json.dumps([
            {"speaker": "NARRATOR", "text": "done", "instruct": "neutral"}
        ])

        def create(**kwargs):
            calls.append(kwargs["max_tokens"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=valid), finish_reason="length")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams(
            max_tokens=4096, context_length=2000, hard_max_tokens=16384)

        result = generate_script.call_llm_for_entries(
            client, "model", "system", "text", params,
            "test_responses.log", "TEST", max_retries=2)

        self.assertEqual([], result)
        self.assertEqual(1, len(calls))
        self.assertLess(calls[0], params.max_tokens)

    def test_hard_capped_truncation_does_not_retry(self):
        calls = []
        valid = json.dumps([
            {"speaker": "NARRATOR", "text": "done", "instruct": "neutral"}
        ])

        def create(**kwargs):
            calls.append(kwargs["max_tokens"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=valid), finish_reason="length")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams(max_tokens=4096, hard_max_tokens=4096)

        result = generate_script.call_llm_for_entries(
            client, "model", "system", "text", params,
            "test_responses.log", "TEST", max_retries=2)

        self.assertEqual([], result)
        self.assertEqual([4096], calls)

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

    def test_review_help_does_not_advertise_unimplemented_source_mode(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent.joinpath("review_script.py")),
             "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--source", result.stdout)


class ReviewDiffAlignmentTests(unittest.TestCase):
    @staticmethod
    def entry(text, speaker="NARRATOR", instruct="neutral"):
        return {"text": text, "speaker": speaker, "instruct": instruct}

    def test_middle_insertion_does_not_shift_later_comparisons(self):
        original = [self.entry("first"), self.entry("second"), self.entry("third")]
        corrected = [
            self.entry("first"), self.entry("inserted", "SUBARU", "urgent"),
            self.entry("second"), self.entry("third"),
        ]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 0)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(stats["speaker_changed"], 0)
        self.assertEqual(stats["instruct_changed"], 0)

    def test_middle_removal_does_not_shift_later_comparisons(self):
        original = [self.entry("first"), self.entry("removed"), self.entry("third")]
        corrected = [self.entry("first"), self.entry("third")]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 0)
        self.assertEqual(stats["entries_removed"], 1)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(stats["speaker_changed"], 0)
        self.assertEqual(stats["instruct_changed"], 0)

    def test_rewrite_and_metadata_changes_remain_visible(self):
        original = [self.entry("old wording"), self.entry("same text")]
        corrected = [
            self.entry("new wording"),
            self.entry("same text", speaker="SUBARU", instruct="quietly"),
        ]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["text_changed"], 1)
        self.assertEqual(stats["speaker_changed"], 1)
        self.assertEqual(stats["instruct_changed"], 1)
        self.assertEqual(stats["entries_changed"], 2)
        self.assertEqual(highlights["text"][0]["before"], "old wording")
        self.assertEqual(highlights["text"][0]["after"], "new wording")

    def test_insertion_does_not_pair_adjacent_july_report_lines(self):
        spirit_explanation = "That’s the tricky thing about spirit mages."
        old_man_question = "By the way, old man, what is it you’re planning on doing?"
        original = [self.entry(old_man_question), self.entry("following line")]
        corrected = [
            self.entry(spirit_explanation), self.entry(old_man_question),
            self.entry("following line"),
        ]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(highlights["text"], [])

    def test_replacement_with_insertion_counts_each_kind_once(self):
        original = [self.entry("first"), self.entry("old"), self.entry("last")]
        corrected = [
            self.entry("first"), self.entry("new"), self.entry("extra"), self.entry("last"),
        ]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["text_changed"], 1)
        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 0)

    def test_addition_and_removal_are_counted_when_length_is_unchanged(self):
        original = [self.entry("first"), self.entry("removed"), self.entry("anchor")]
        corrected = [self.entry("inserted"), self.entry("first"), self.entry("anchor")]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 1)
        self.assertEqual(stats["text_changed"], 0)

    def test_highlight_pool_cap_is_preserved(self):
        original = [self.entry(f"old {index}") for index in range(510)]
        corrected = [self.entry(f"new {index}") for index in range(510)]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["text_changed"], 510)
        self.assertEqual(len(highlights["text"]), review_script._MAX_HIGHLIGHT_POOL)

    def test_speaker_change_includes_entry_number_and_neighbor_context(self):
        original = [
            self.entry("before"), self.entry("He said as the cart passed."), self.entry("after"),
        ]
        corrected = [
            self.entry("before"), self.entry("He said as the cart passed.", speaker="KENJI"),
            self.entry("after"),
        ]
        highlights = {"text": [], "speaker": []}

        review_script.diff_entries(original, corrected, highlights, entry_offset=100)

        change = highlights["speaker"][0]
        self.assertEqual(change["entry_number"], 102)
        self.assertEqual(change["context_before"], "before")
        self.assertEqual(change["context_after"], "after")
        self.assertIn("Narrator-to-character", change["manual_review_reason"])

    def test_character_to_character_change_is_not_automatically_flagged(self):
        original = [self.entry("Hello", speaker="MAN")]
        corrected = [self.entry("Hello", speaker="KENJI")]
        highlights = {"text": [], "speaker": []}

        review_script.diff_entries(original, corrected, highlights)

        self.assertNotIn("manual_review_reason", highlights["speaker"][0])

    def test_speaker_markdown_does_not_assert_correction(self):
        highlights = {"text_rewrites": [], "speaker_changes": [{
            "text": "He said as the cart passed.", "before": "NARRATOR", "after": "KENJI",
            "entry_number": 102, "context_before": "Before.", "context_after": "After.",
            "manual_review_reason": "Narrator-to-character changes alter the reading voice.",
        }]}

        markdown = "\n".join(core._markdown_diff_highlights_lines(highlights))

        self.assertIn("Speaker changes to verify", markdown)
        self.assertIn("Entry 102", markdown)
        self.assertIn("Previous", markdown)
        self.assertIn("Manual check recommended", markdown)
        self.assertNotIn("corrected to", markdown)


class ReviewFailureReportingTests(unittest.TestCase):
    def test_failed_section_uses_human_entry_range_and_stable_ratio(self):
        section = review_script.get_failed_section(
            batch=3, zero_based_start=50, length=25,
            category="text_length_mismatch", word_ratio=0.912345,
        )

        self.assertEqual(section, {
            "batch": 3,
            "entry_start": 51,
            "entry_end": 75,
            "category": "text_length_mismatch",
            "word_ratio": 0.9123,
        })

    def test_failed_sections_parser_and_markdown_explain_safe_retry(self):
        lines = [
            'FAILED_SECTIONS_JSON: {"sections":[{"batch":3,"entry_start":51,'
            '"entry_end":75,"category":"text_length_mismatch","word_ratio":0.91}],'
            '"original_entries_preserved":true,"checkpoint_retained":true,'
            '"retry_from_batch":3}'
        ]

        failures = core._extract_failed_sections(lines)
        markdown = core._markdown_failed_sections_lines(failures)

        self.assertEqual(failures["sections"][0]["entry_start"], 51)
        self.assertTrue(any("entries 51–75" in line for line in markdown))
        self.assertTrue(any("original entries" in line for line in markdown))
        self.assertTrue(any("single-book review" in line for line in markdown))

    def test_malformed_failed_sections_are_not_reported(self):
        self.assertEqual(
            core._extract_failed_sections(['FAILED_SECTIONS_JSON: {"sections":"bad"}']),
            {"sections": []},
        )


class ReviewVramTests(unittest.TestCase):
    def test_watchdog_checks_both_gpu_backends_and_keeps_zero_usage(self):
        amd = {"card0": {"VRAM Total Used Memory (B)": 20 * 1024**2,
                         "VRAM Total Memory (B)": 100 * 1024**2},
               "card1": {"VRAM Total Used Memory (B)": 60 * 1024**2,
                         "VRAM Total Memory (B)": 100 * 1024**2}}
        for nvidia, expected in (("95,100\n10,100", (95, 100)),
                                 ("10,100", (60, 100)),
                                 ("malformed", (60, 100))):
            with self.subTest(nvidia=nvidia), \
                 patch.object(review_script, "run_rocm_smi_json", return_value=amd), \
                 patch.object(review_script.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=nvidia)) as probe:
                self.assertEqual(tuple(value * 1024**2 for value in expected),
                                 review_script.get_vram_usage())
                probe.assert_called_once()
        amd["card0"]["VRAM Total Used Memory (B)"] = 0
        with patch.object(review_script, "run_rocm_smi_json", return_value={"card0": amd["card0"]}), \
             patch.object(review_script.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual((0, 100 * 1024**2), review_script.get_vram_usage())
        with patch.object(review_script, "run_rocm_smi_json", return_value=None), \
             patch.object(review_script.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(review_script.get_vram_usage())

    def run_review(self, path, headroom, review, dedupe, executor=None, remote=False, generation=None):
        from contextlib import ExitStack, redirect_stdout
        from io import StringIO
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            patches = {
                "load_app_config": {"generation": {"review_batch_size": 1, **(generation or {})}},
                "get_active_llm_config": {},
                "ensure_ideal_settings": (remote, {}, "test settings"),
                "get_current_status": {"loaded": False},
                "make_run_client": object(),
                "get_cached_or_benchmarked_concurrency": 2 if executor else 1,
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(review_script, name, return_value=value))
            stack.enter_context(patch.object(review_script, "wait_for_vram_headroom",
                                            side_effect=headroom))
            stack.enter_context(patch.object(review_script, "review_batch", side_effect=review))
            stack.enter_context(patch.object(review_script, "dedupe_speakers", side_effect=dedupe))
            stack.enter_context(patch.object(sys, "argv", ["review", "--input", str(path),
                                                          "--dedupe-speakers"]))
            if executor:
                stack.enter_context(patch.object(review_script, "ThreadPoolExecutor", executor))
            review_script.main()

    def test_out_of_order_vram_skip_is_retried_from_saved_checkpoint(self):
        class ReverseStartExecutor:
            def __init__(self, **_kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def map(self, function, items):
                return list(reversed([function(item) for item in reversed(list(items))]))

        original = [{"speaker": "ALICE", "text": text, "instruct": "neutral"}
                    for text in ("First line.", "Second line.")]
        calls = []
        def review(_client, _model, batch, index, *_args, **_kwargs):
            calls.append(index)
            return [dict(item, speaker="BETTY") for item in batch]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            path.write_text(json.dumps(original))
            dedupe = MagicMock(return_value=({}, 0, []))
            self.run_review(path, [True, False], review, dedupe, ReverseStartExecutor)
            self.assertEqual([2], calls)
            dedupe.assert_not_called()
            self.assertEqual(["ALICE", "BETTY"],
                             [item["speaker"] for item in json.loads(path.read_text())])
            raw = json.loads(Path(str(path) + ".review_checkpoint.json").read_text())
            self.assertEqual([1], raw["failed_batches"])
            self.assertEqual([1, 1], raw["batch_lengths"])
            resumed = review_script.load_checkpoint(
                str(path), 2, 1, 0, json.loads(path.read_text()))
            self.assertEqual(0, resumed["completed_batches"])
            self.assertEqual([], resumed["all_corrected"])
            self.run_review(path, [True, True, True], review, dedupe)
            self.assertEqual([2, 1, 2], calls)
            self.assertEqual(["BETTY", "BETTY"],
                             [item["speaker"] for item in json.loads(path.read_text())])
            self.assertFalse(Path(str(path) + ".review_checkpoint.json").exists())

    def test_dedupe_rechecks_headroom_and_can_resume_without_repeating_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            entry = {"speaker": "ALICE", "text": "One line.", "instruct": "neutral"}
            path.write_text(json.dumps([entry]))
            review = MagicMock(return_value=[dict(entry, speaker="BETTY")])
            dedupe = MagicMock(return_value=({}, 0, []))
            self.run_review(path, [True, False], review, dedupe)
            review.assert_called_once()
            dedupe.assert_not_called()
            self.assertTrue(Path(str(path) + ".review_checkpoint.json").exists())
            self.assertEqual("BETTY", json.loads(path.read_text())[0]["speaker"])
            review.reset_mock()
            self.run_review(path, [True], review, dedupe)
            review.assert_not_called()
            dedupe.assert_called_once()
            self.assertFalse(Path(str(path) + ".review_checkpoint.json").exists())

    def test_remote_review_does_not_probe_local_headroom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            entry = {"speaker": "ALICE", "text": "One line.", "instruct": "neutral"}
            path.write_text(json.dumps([entry]))
            headroom = MagicMock(side_effect=AssertionError("local GPU must not be probed"))
            self.run_review(path, headroom, MagicMock(return_value=[entry]),
                            MagicMock(return_value=({}, 0, [])), remote=True)
            headroom.assert_not_called()

    def test_legacy_minimal_entry_and_extra_metadata_remain_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            entry = {"type": "NARRATOR", "text": "One line.", "extra": {"note": "keep"}}
            path.write_text(json.dumps([entry]))
            self.run_review(path, [True, True], MagicMock(return_value=[entry]),
                            MagicMock(return_value=({}, 0, [])))
            self.assertEqual([entry], json.loads(path.read_text()))


class ReviewSummaryTests(unittest.TestCase):
    def test_incomplete_summary_is_deterministic_and_does_not_call_llm(self):
        stats = {"total_changes": 12, "batches_failed": 1, "batches_skipped_vram": 0}
        with patch.object(core, "_llm_summarize_report") as summarize:
            lines = core._insert_llm_summary(["Report"], 1, stats, incomplete=True)

        summarize.assert_not_called()
        self.assertIn("This review was incomplete", lines[4])
        self.assertIn("1 section(s) failed", lines[4])
        self.assertIn("12 change(s)", lines[4])

    def test_unsupported_quality_claim_uses_deterministic_fallback(self):
        stats = {"total_changes": 3}
        with patch.object(
                core, "_llm_summarize_report",
                return_value="Everything looks great and all issues were fixed."):
            lines = core._insert_llm_summary(["Report"], 1, stats, allow_llm=True)

        self.assertIn("without recorded failed or skipped sections", lines[4])
        self.assertNotIn("Everything looks great", lines[4])

    def test_evidence_bound_llm_summary_is_kept_for_complete_run(self):
        summary = "The pass reported three text changes; inspect the examples below."
        with patch.object(core, "_llm_summarize_report", return_value=summary):
            lines = core._insert_llm_summary(["Report"], 1, {"total_changes": 3}, allow_llm=True)

        self.assertEqual(lines[4], summary)


class ReviewChangeDensityTests(unittest.TestCase):
    def test_high_change_density_warns_with_separate_structural_counts(self):
        stats = {
            "entries_before": 2615, "entries_changed": 613,
            "entries_added": 8, "entries_removed": 1,
        }

        lines = core._markdown_change_density_lines(stats)

        self.assertEqual(len(lines), 1)
        self.assertIn("613 of 2615", lines[0])
        self.assertIn("23.4%", lines[0])
        self.assertIn("+8/-1", lines[0])
        self.assertIn("before generating audio", lines[0])

    def test_low_density_and_small_scripts_do_not_warn(self):
        self.assertEqual(core._markdown_change_density_lines({
            "entries_before": 1000, "entries_changed": 199,
        }), [])
        self.assertEqual(core._markdown_change_density_lines({
            "entries_before": 99, "entries_changed": 99,
        }), [])


class DedupeSpeakersResolverTests(unittest.TestCase):
    """Covers the Area 5 fix: dedupe_speakers' registry pre-seed must resolve
    a punctuation/spacing variant to the label actually used in this script,
    the same way generation's _identity_key normalization does."""

    def test_dedupe_uses_profile_retry_controls_and_recovers_rate_limit(self):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                error = RuntimeError("rate limited")
                error.status_code = 429
                raise error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"ALICE":"BETTY"}'), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams(
            seed=0, provider_extra_body={"custom_sampler": True}, structured_output="off",
            api_retry_limit=1, retry_initial_delay_seconds=0, context_length=8192)
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY")]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generate_script, "get_response_log_path", return_value=str(Path(tmp, "object.log"))):
            result = review_script.dedupe_speakers(client, "model", entries, params=params,
                                                   context_length=8192)
            log = Path(tmp, "object.log").read_text()
        self.assertEqual(({"ALICE": "BETTY"}, 1, [(0, "speaker", "BETTY")]), result)
        self.assertEqual(2, len(requests))
        self.assertTrue(all(request["extra_body"]["seed"] == 0 for request in requests))
        self.assertTrue(all(request["extra_body"]["custom_sampler"] for request in requests))
        self.assertIn("SPEAKER DEDUPE", log)
        self.assertEqual(8192, params.context_length)
        self.assertEqual(4096, params.max_tokens)

    def test_empty_dedupe_map_is_an_accepted_object_without_retry(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="{}"), finish_reason="stop")], usage=None)
        create = MagicMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY")]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generate_script, "get_response_log_path", return_value=str(Path(tmp, "object.log"))):
            self.assertEqual(({}, 0, []), review_script.dedupe_speakers(client, "model", entries))
        create.assert_called_once()

    def test_dedupe_api_exhaustion_retains_known_aliases_with_bounded_attempts(self):
        def unavailable(**_kwargs):
            error = RuntimeError("unavailable")
            error.status_code = 503
            raise error
        create = MagicMock(side_effect=unavailable)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY")]
        params = generate_script.LLMGenParams(api_retry_limit=1, retry_initial_delay_seconds=0)
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, "aliases.json")
            registry.write_text('{"ALICE":"BETTY"}')
            result = review_script.dedupe_speakers(client, "model", entries,
                                                   registry_path=str(registry), params=params)
            self.assertEqual({"ALICE": "BETTY"}, json.loads(registry.read_text()))
        self.assertEqual(2, create.call_count)
        self.assertEqual(({"ALICE": "BETTY"}, 1, [(0, "speaker", "BETTY")]), result)

    def test_wrong_registry_shapes_refuse_before_model_and_preserve_bytes(self):
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY")]
        for malformed in (["ALICE", "BETTY"], {"ALICE": ["BETTY"]},
                          {"ALICE": 7}, {"ALICE": None}):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmp:
                registry_path = Path(tmp, "aliases.json")
                original = json.dumps(malformed, indent=3).encode()
                registry_path.write_bytes(original)
                client = MagicMock()
                with self.assertRaisesRegex(ValueError, "Invalid alias registry"):
                    review_script.dedupe_speakers(client, "model", entries,
                                                 registry_path=str(registry_path))
                client.chat.completions.create.assert_not_called()
                self.assertEqual(original, registry_path.read_bytes())

    def test_malformed_input_exits_before_model_or_output_changes(self):
        for entry in ({"speaker": "NARRATOR"}, {"text": None}, {"text": 7},
                      {"text": "line", "speaker": []},
                      {"text": "line", "type": {}},
                      {"text": "line", "instruct": []}):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "script.json")
                source = json.dumps([entry])
                path.write_text(source)
                checkpoint = Path(str(path) + ".review_checkpoint.json")
                checkpoint.write_text("preserve checkpoint")
                with patch.object(sys, "argv", ["review", "--input", str(path)]), \
                     patch.object(review_script, "load_app_config", side_effect=AssertionError(
                         "invalid input must stop before configuration or inference")), \
                     patch("builtins.print") as warning:
                    with self.assertRaises(SystemExit) as stopped:
                        review_script.main()
                self.assertEqual(1, stopped.exception.code)
                self.assertTrue(any("entry 1" in str(call) for call in warning.call_args_list))
                self.assertEqual(source, path.read_text())
                self.assertEqual("preserve checkpoint", checkpoint.read_text())

    def test_empty_script_exits_before_configuration_or_output_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            path.write_text("[]")
            checkpoint = Path(str(path) + ".review_checkpoint.json")
            checkpoint.write_text("preserve checkpoint")
            with patch.object(sys, "argv", ["review", "--input", str(path)]), \
                 patch.object(review_script, "load_app_config", side_effect=AssertionError(
                     "empty script must stop before configuration or inference")), \
                 patch("builtins.print") as warning:
                with self.assertRaises(SystemExit) as stopped:
                    review_script.main()
            self.assertEqual(1, stopped.exception.code)
            self.assertTrue(any("at least one" in str(call) for call in warning.call_args_list))
            self.assertEqual("[]", path.read_text())
            self.assertEqual("preserve checkpoint", checkpoint.read_text())

    def test_group_and_unknown_targets_are_rejected_for_model_and_registry(self):
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY", "TWINS", "CROWD", "RAM AND REM")]
        before = json.loads(json.dumps(entries))
        for source in ("model", "registry"):
            for proposal in ({"ALICE": "TWINS"}, {"TWINS": "CROWD"},
                             {"RAM AND REM": "ALICE"}, {"ALICE": "UNKNOWN"}):
                with self.subTest(source=source, proposal=proposal), tempfile.TemporaryDirectory() as tmp:
                    registry_path = os.path.join(tmp, "aliases.json")
                    with open(registry_path, "w", encoding="utf-8") as f:
                        json.dump(proposal if source == "registry" else {}, f)
                    client = MagicMock()
                    client.chat.completions.create.return_value.choices[0].message.content = json.dumps(
                        proposal if source == "model" else {})
                    mapping, renamed, changes = review_script.dedupe_speakers(
                        client, "model", entries, registry_path=registry_path)
                    self.assertEqual(({}, 0, []), (mapping, renamed, changes))
                    self.assertEqual(before, entries)
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = '{"ALICE":"BETTY"}'
        self.assertEqual(({"ALICE": "BETTY"}, 1, [(0, "speaker", "BETTY")]),
                         review_script.dedupe_speakers(client, "model", entries))

    def test_registry_alias_resolves_via_shared_identity_normalization(self):
        entries = [
            {"speaker": "MR. SMITH", "text": "Hello there, stranger."},
            {"speaker": "JOHN SMITH", "text": "Another line of dialogue."},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "aliases.json")
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump({"MR SMITH": "JOHN SMITH"}, f)

            # Force the LLM step to fail so only the registry's forced aliases
            # apply -- isolates the resolver fix from any model behavior.
            client = MagicMock()
            client.chat.completions.create.side_effect = RuntimeError("LLM unavailable")

            mapping, renamed, changes = review_script.dedupe_speakers(
                client, "model", entries, registry_path=registry_path)

        self.assertEqual({"MR. SMITH": "JOHN SMITH"}, mapping)
        self.assertEqual(1, renamed)
        self.assertEqual([(0, "speaker", "JOHN SMITH")], changes)

    def test_registry_chain_resolves_even_when_intermediate_label_is_absent(self):
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "CAROL")]
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, "aliases.json")
            registry.write_text('{"ALICE":"BETTY","BETTY":"CAROL"}')
            client = MagicMock()
            client.chat.completions.create.side_effect = RuntimeError("offline")
            self.assertEqual(({"ALICE": "CAROL"}, 1, [(0, "speaker", "CAROL")]),
                             review_script.dedupe_speakers(
                                 client, "model", entries, registry_path=str(registry)))
            self.assertEqual({"ALICE": "BETTY", "BETTY": "CAROL"},
                             json.loads(registry.read_text()))

    def test_model_chain_flattens_and_known_registry_target_wins(self):
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY", "CAROL", "DIANA")]
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp, "aliases.json")
            registry.write_text('{"BETTY":"DIANA"}')
            client = MagicMock()
            client.chat.completions.create.return_value.choices[0].message.content = (
                '{"ALICE":"BETTY","BETTY":"CAROL"}')
            self.assertEqual(({"ALICE": "DIANA", "BETTY": "DIANA"}, 2,
                              [(0, "speaker", "DIANA"), (1, "speaker", "DIANA")]),
                             review_script.dedupe_speakers(
                                 client, "model", entries, registry_path=str(registry)))

    def test_cycles_and_aliases_entering_them_are_rejected_without_swapping(self):
        entries = [{"speaker": name, "text": "A line of dialogue."}
                   for name in ("ALICE", "BETTY", "CAROL", "DIANA", "ELLEN")]
        for source in ("registry", "model"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                mapping = {"ALICE": "BETTY", "BETTY": "alice", "CAROL": "ALICE", "DIANA": "ELLEN"}
                registry = Path(tmp, "aliases.json")
                registry.write_text(json.dumps(mapping if source == "registry" else {}))
                client = MagicMock()
                client.chat.completions.create.return_value.choices[0].message.content = json.dumps(
                    mapping if source == "model" else {})
                original = registry.read_bytes()
                if source == "registry":
                    with self.assertRaisesRegex(ValueError, "cycle"):
                        review_script.dedupe_speakers(client, "model", entries, registry_path=str(registry))
                    client.chat.completions.create.assert_not_called()
                    self.assertEqual(original, registry.read_bytes())
                else:
                    with patch("builtins.print") as warning:
                        result = review_script.dedupe_speakers(
                            client, "model", entries, registry_path=str(registry))
                    self.assertEqual(({"DIANA": "ELLEN"}, 1, [(3, "speaker", "ELLEN")]), result)
                    self.assertTrue(any("cycle" in str(call).lower() for call in warning.call_args_list))



class NarratorMetadataMergeTests(unittest.TestCase):
    def test_actual_review_output_keeps_pause_and_metadata_boundaries(self):
        from project import group_into_chunks
        entries = [{"speaker": "NARRATOR", "text": "First ordinary line.",
                    "instruct": "neutral", "pause_after": 900, "spoken": False},
                   {"speaker": "NARRATOR", "text": "Second ordinary line.",
                    "instruct": "neutral", "spoken": True}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "script.json")
            path.write_text(json.dumps(entries))
            dedupe = MagicMock(return_value=({}, 0, []))
            ReviewVramTests.run_review(
                self, path, [True, True, True],
                lambda _client, _model, batch, *_args, **_kwargs: batch,
                dedupe, generation={"merge_narrators": True})
            saved = json.loads(path.read_text())
        self.assertEqual(entries, saved)
        chunks = group_into_chunks(saved)
        self.assertEqual(2, len(chunks))
        self.assertEqual(900, chunks[0]["pause_after"])

    def test_unmerged_narrator_retains_all_metadata(self):
        entry = {"speaker": "NARRATOR", "text": "An ordinary line.",
                 "pause_after": 900, "spoken": False, "extra": {"note": "keep"}}
        self.assertEqual(([entry], 0), review_script.merge_consecutive_narrators([entry]))

    def test_metadata_differences_and_pauses_keep_entry_boundaries(self):
        first = {"speaker": "NARRATOR", "text": "First ordinary line.", "instruct": "neutral"}
        second = dict(first, text="Second ordinary line.")
        for left, right in ((dict(first, spoken=False), dict(second, spoken=True)),
                            (first, dict(second, pause_after=800)),
                            (dict(first, pause_after=800), dict(second, pause_after=800)),
                            (dict(first, evidence={"line": 1}), dict(second, evidence={"line": 2}))):
            with self.subTest(left=left, right=right):
                self.assertEqual(([left, right], 0),
                                 review_script.merge_consecutive_narrators([left, right]))

    def test_compatible_metadata_merges_without_mutating_input(self):
        original = [{"speaker": "NARRATOR", "text": text, "instruct": "neutral",
                     "spoken": False, "pause_after": 0, "extra": {"note": "keep"}}
                    for text in ("First ordinary line.", "Second ordinary line.")]
        before = json.loads(json.dumps(original))
        result, count = review_script.merge_consecutive_narrators(original)
        self.assertEqual(1, count)
        self.assertEqual([dict(original[0], text="First ordinary line. Second ordinary line.")], result)
        self.assertEqual(before, original)
        plain = [{"speaker": "NARRATOR", "text": e["text"], "instruct": "neutral"}
                 for e in original]
        self.assertEqual(([dict(plain[0], text=result[0]["text"])], 1),
                         review_script.merge_consecutive_narrators(plain))

    def test_registry_keeps_alias_added_during_llm_call(self):
        entries = [
            {"speaker": "BOB", "text": "Hello there."},
            {"speaker": "ROBERT", "text": "Hello again."},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "aliases.json")
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

            def concurrent_update(**kwargs):
                with open(registry_path, "w", encoding="utf-8") as f:
                    json.dump({"BETH": "ELIZABETH"}, f)
                response = MagicMock()
                response.choices[0].message.content = '{"BOB": "ROBERT"}'
                return response

            client = MagicMock()
            client.chat.completions.create.side_effect = concurrent_update
            review_script.dedupe_speakers(client, "model", entries,
                                          registry_path=registry_path)
            with open(registry_path, "r", encoding="utf-8") as f:
                self.assertEqual({"BETH": "ELIZABETH", "BOB": "ROBERT"}, json.load(f))


class PassSpecificSalvageTests(unittest.TestCase):
    def test_salvages_pass2_and_pass3_object_shapes(self):
        pass2 = ('[{"n":0,"head":"Tell me now","speaker":"ELENA"},'
                 '{"n":1,"head":"The room was","speaker":"NARRATOR"}, BROKEN]')
        pass3 = ('[{"n":0,"head":"Tell me now","instruct":"Firm."},'
                 '{"n":1,"head":"The room was","instruct":"Neutral."}, BROKEN]')
        self.assertEqual(["ELENA", "NARRATOR"],
                         [e["speaker"] for e in generate_script.salvage_json_entries(pass2)])
        self.assertEqual(["Firm.", "Neutral."],
                         [e["instruct"] for e in generate_script.salvage_json_entries(pass3)])

    def test_single_pass_chunk_size_validates_config_and_cli(self):
        self.assertEqual(3000, generate_script.get_valid_chunk_size(3000))
        self.assertEqual(500, generate_script.get_valid_chunk_size(3000, 500))
        for config_value in (0, -1, "3000", True):
            with self.assertRaises(ValueError):
                generate_script.get_valid_chunk_size(config_value)

    def test_review_rejects_text_only_salvage_even_when_word_count_matches(self):
        original = [{"speaker": "NARRATOR", "text": "one two three four five",
                     "instruct": "Neutral."}]
        client = LlmReviewTests._client_with_responses([
            '[{"text":"one two three four five"}, BROKEN]'])
        params = generate_script.LLMGenParams(max_tokens=100, temperature=0.1)
        self.assertIsNone(review_script.review_batch(
            client, "m", original, 1, 1, params, max_retries=0))

    def test_review_retries_dropped_text_then_accepts_complete_restructure(self):
        original = [
            {"speaker": "NARRATOR", "text": "He answered.", "instruct": "Neutral."},
            {"speaker": "A", "text": "Come with me.", "instruct": "Firm."},
        ]
        dropped = [{"speaker": "A", "text": "Come with me.", "instruct": "Firm."}]
        restructured = [{
            "speaker": "NARRATOR", "text": "He answered.   Come with me.",
            "instruct": "Neutral.",
        }]
        client = LlmReviewTests._client_with_responses([
            json.dumps(dropped), json.dumps(restructured)])
        params = generate_script.LLMGenParams(max_tokens=100, temperature=0.1)

        result = review_script.review_batch(
            client, "m", original, 1, 1, params, max_retries=1)

        self.assertEqual(restructured, result)

    def test_review_rejects_same_length_shift_with_duplicate_and_drop(self):
        original = [
            {"speaker": "A", "text": "First line.", "instruct": "One."},
            {"speaker": "B", "text": "Second line.", "instruct": "Two."},
        ]
        shifted = [
            {"speaker": "A", "text": "First line.", "instruct": "One."},
            {"speaker": "A", "text": "First line.", "instruct": "One."},
        ]
        client = LlmReviewTests._client_with_responses([json.dumps(shifted)])
        params = generate_script.LLMGenParams(max_tokens=100, temperature=0.1)

        self.assertIsNone(review_script.review_batch(
            client, "m", original, 1, 1, params, max_retries=0))


class ReviewAliasPublicationSafetyTests(unittest.TestCase):
    def test_latest_human_graph_controls_saved_aliases_and_returned_changes(self):
        import copy
        cases = [
            ({}, {'ALICE':'DIANA'}, {'ALICE':'BETTY'}, {'ALICE':'DIANA'}, {'ALICE':'DIANA'}),
            ({'BETTY':'CAROL'}, {'BETTY':'DIANA'}, {'ALICE':'BETTY'}, {'BETTY':'DIANA','ALICE':'DIANA'}, {'ALICE':'DIANA','BETTY':'DIANA'}),
            ({'ALICE':'BETTY'}, {}, {'ALICE':'CAROL'}, {}, {}),
            ({}, {'BETTY':'ALICE'}, {'ALICE':'BETTY'}, {'BETTY':'ALICE'}, {'BETTY':'ALICE'}),
        ]
        entries=[{'speaker':name,'text':'A line of dialogue.'} for name in ('ALICE','BETTY','CAROL','DIANA')]
        before=copy.deepcopy(entries)
        for initial,late,proposal,expected_file,expected_map in cases:
            with self.subTest(initial=initial,late=late),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp,'aliases.json');path.write_text(json.dumps(initial))
                def response(*args,**kwargs):
                    with review_script.file_lock(str(path)):
                        review_script.atomic_json_write(late,str(path))
                    return proposal
                with patch.object(review_script,'call_llm_for_object',side_effect=response):
                    mapping,count,changes=review_script.dedupe_speakers(object(),'fixture',entries,registry_path=str(path))
                self.assertEqual(expected_file,json.loads(path.read_text()))
                self.assertEqual(expected_map,mapping)
                expected_changes=[(i,'speaker',expected_map[e['speaker']]) for i,e in enumerate(entries) if e['speaker'] in expected_map]
                self.assertEqual(expected_changes,changes);self.assertEqual(len(expected_changes),count)
                self.assertEqual(before,entries)
                assert_file_lock_released(str(path))

    def test_invalid_human_registry_is_preserved_before_and_after_model_call(self):
        entries=[{'speaker':name,'text':'A line of dialogue.'} for name in ('ALICE','BETTY')]
        for raw in (b'broken',b'null',b'[]',b'{"ALICE":"BETTY","BETTY":"ALICE"}'):
            for late in (False,True):
                with self.subTest(raw=raw,late=late),tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp,'aliases.json');path.write_bytes(b'{}' if late else raw)
                    def response(*args,**kwargs):
                        path.write_bytes(raw)
                        return {'ALICE':'BETTY'}
                    with patch.object(review_script,'call_llm_for_object',side_effect=response) as model:
                        with self.assertRaises(ValueError):
                            review_script.dedupe_speakers(object(),'fixture',entries,registry_path=str(path))
                    self.assertEqual(raw,path.read_bytes())
                    self.assertEqual(int(late),model.call_count)
                    assert_file_lock_released(str(path))

    def test_new_roots_require_roster_or_human_approval_and_human_bytes_survive(self):
        entries=[{'speaker':name,'text':'A line of dialogue.'} for name in ('ALICE','BETTY')]
        for initial,proposal,expected in (({}, {'ALICE':'INVENTED'},{}),
                ({'OTHER':'HUMAN ROOT'},{'ALICE':'OTHER'},{'ALICE':'HUMAN ROOT'}),
                ({' OTHER ':' HUMAN ROOT '},{'ALICE':'OTHER'},{'ALICE':'HUMAN ROOT'}),
                ({'ALICE':'BETTY','BETTY':'HUMAN ROOT'},{},{'ALICE':'HUMAN ROOT','BETTY':'HUMAN ROOT'})):
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp,'aliases.json');raw=json.dumps(initial,indent=3).encode();path.write_bytes(raw)
                with patch.object(review_script,'call_llm_for_object',return_value=proposal):
                    mapping,_,_=review_script.dedupe_speakers(object(),'fixture',entries,registry_path=str(path))
                self.assertEqual(expected,mapping)
                saved=json.loads(path.read_text())
                for key,value in initial.items():self.assertEqual(value,saved[key])
                if not proposal or not expected:self.assertEqual(raw,path.read_bytes())

    def test_normalized_conflicting_model_proposals_do_not_choose_a_winner(self):
        entries=[{'speaker':name,'text':'A line of dialogue.'} for name in ('ALICE','BETTY','CAROL')]
        with patch.object(review_script,'call_llm_for_object',return_value={'ALICE':'BETTY','alice':'CAROL'}):
            self.assertEqual(({},0,[]),review_script.dedupe_speakers(object(),'fixture',entries))
