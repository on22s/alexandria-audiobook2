"""Every LLM request is logged next to its reply, so a rejected answer can be
replayed exactly.

WHAT THIS VERIFIES. call_llm_for_entries writes each attempt's exact messages
(system, user, and the retry-feedback user turn when there is one) to
<log>.requests.log, under a request id that the matching llm_responses.log
block also carries. A Pass 2 "spoken line not named" rejection records what
the model answered, and that value reaches the printed warning but NOT the
retry feedback the model is sent, so logging never changes the prompt. With
ALEXANDRIA_RUN_ID set, the request log sits beside the run's response log.
Synthetic text only.
"""
import contextlib
import errno
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI

import generate_script as gs
from pass_quality import validate_attribution


def make_client(replies):
    replies = list(replies)
    sent = []

    def create(**kwargs):
        sent.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=replies.pop(0)), finish_reason="stop")], usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


FROZEN = [{"type": "SPOKEN", "text": "I should like to come with you,"},
          {"type": "NARRATOR", "text": "said the lad."}]
SHIFTED = json.dumps([{"n": 0, "speaker": "NARRATOR"}, {"n": 1, "speaker": "TOM"}])
CORRECT = json.dumps([{"n": 0, "speaker": "TOM"}, {"n": 1, "speaker": "NARRATOR"}])


def blocks(path):
    return [block.strip() for block in Path(path).read_text(encoding="utf-8").split("=" * 80)
            if block.strip()]


class RequestLogTests(unittest.TestCase):
    def run_sdk_call(self, tmp, handler, params=None, max_retries=0, *,
                     validate=None, system_prompt="system text", user_prompt="user text"):
        params = params or gs.LLMGenParams(
            "system text", "", 8192, 0.0, 1, api_retry_limit=0)
        output, attempts = io.StringIO(), []
        with OpenAI(base_url="http://fixture.invalid/v1", api_key="synthetic",
                    max_retries=0, http_client=httpx.Client(
                        transport=httpx.MockTransport(handler))) as client, \
                patch.dict(os.environ, {"ALEXANDRIA_RUN_ID": ""}), \
                patch.object(gs, "get_response_log_path",
                             side_effect=lambda name: os.path.join(tmp, name)), \
                contextlib.redirect_stdout(output):
            result = gs.call_llm_for_entries(
                client, "fixture-model", system_prompt, user_prompt, params,
                "llm_responses.log", "ATTRIBUTE", max_retries=max_retries,
                attempt_observer=attempts.append, validate_entries=validate)
        return result, output.getvalue(), attempts

    def sdk_response(self, content=CORRECT):
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "fixture-model", "choices": [{"index": 0,
                "finish_reason": "stop", "message": {
                    "role": "assistant", "content": content}}]})

    def run_call(self, tmp, replies, validate=None, max_retries=0, run_id=""):
        client, sent = make_client(replies)
        params = gs.LLMGenParams("system text", "", 8192, 0.0, 1)
        out = io.StringIO()
        with patch.dict(os.environ, {"ALEXANDRIA_RUN_ID": run_id}), \
                patch.object(gs, "get_response_log_path",
                             side_effect=lambda name: os.path.join(tmp, name)), \
                contextlib.redirect_stdout(out):
            result = gs.call_llm_for_entries(
                client, "fixture-model", "system text", "user text", params,
                "llm_responses.log", "ATTRIBUTE", max_retries=max_retries,
                validate_entries=validate)
        return result, sent, out.getvalue()

    def test_request_is_logged_with_the_id_its_response_carries(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_call(tmp, [CORRECT])
            [request] = blocks(os.path.join(tmp, "llm_responses.requests.log"))
            [response] = blocks(os.path.join(tmp, "llm_responses.log"))
            request_id = re.search(r"request=([0-9a-f]{12})", request).group(1)
            self.assertIn(f"request={request_id}", response.splitlines()[0])
            self.assertIn("model=fixture-model", request)
            self.assertIn("[system]\nsystem text", request)
            self.assertIn("[user]\nuser text", request)

    def test_retry_logs_the_feedback_turn_actually_sent(self):
        validate = lambda entries: validate_attribution(FROZEN, entries)
        with tempfile.TemporaryDirectory() as tmp:
            _, sent, _ = self.run_call(tmp, [SHIFTED, CORRECT], validate, max_retries=1)
            requests = blocks(os.path.join(tmp, "llm_responses.requests.log"))
            responses = blocks(os.path.join(tmp, "llm_responses.log"))
            self.assertEqual(2, len(requests))
            ids = [re.search(r"request=(\w+)", b).group(1) for b in requests]
            self.assertEqual(ids, [re.search(r"request=(\w+)", b).group(1) for b in responses])
            self.assertNotEqual(ids[0], ids[1])
            self.assertIn(sent[1][1]["content"], requests[1])
            self.assertIn("previous response was rejected", requests[1])

    def test_quality_retry_preserves_index_only_contract_and_budget(self):
        sent = []
        system = 'Return only JSON rows with n and speaker. Never return source text.'
        user = 'Assign a speaker to each numbered entry.'
        def respond(request):
            sent.append(json.loads(request.content))
            return self.sdk_response(SHIFTED if len(sent) == 1 else CORRECT)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, attempts = self.run_sdk_call(tmp, respond, max_retries=1,
                validate=lambda rows: validate_attribution(FROZEN, rows),
                system_prompt=system, user_prompt=user)
            requests = blocks(os.path.join(tmp, 'llm_responses.requests.log'))
        self.assertEqual(json.loads(CORRECT), result)
        self.assertEqual(2, len(sent))
        self.assertEqual([{'role': 'system', 'content': system},
                          {'role': 'user', 'content': user}], sent[0]['messages'])
        self.assertEqual(system, sent[1]['messages'][0]['content'])
        retry = sent[1]['messages'][1]['content']
        self.assertTrue(retry.startswith(user))
        self.assertIn('Return the complete result in the required output format.', retry)
        self.assertNotIn('Return the complete source text', retry)
        self.assertIn('A narrator line must keep the speaker NARRATOR', retry)
        self.assertEqual(sent[0]['max_tokens'], sent[1]['max_tokens'])
        self.assertEqual(['quality_rejected', 'accepted'], [a['outcome'] for a in attempts])
        self.assertIn(retry, requests[1])

    def test_quality_retry_preserves_full_text_fidelity_contract(self):
        import review_script
        source = [{'speaker': 'NARRATOR', 'text': 'The rain stopped.', 'instruct': ''}]
        changed = [{**source[0], 'text': 'The snow stopped.'}]
        sent = []
        system = 'Return all source text in JSON rows without changing any words.'
        def respond(request):
            sent.append(json.loads(request.content))
            return self.sdk_response(json.dumps(changed if len(sent) == 1 else source))
        with tempfile.TemporaryDirectory() as tmp:
            result, _, attempts = self.run_sdk_call(tmp, respond, max_retries=1,
                validate=lambda rows: review_script.get_review_source_binding(source, rows)[1],
                system_prompt=system, user_prompt=json.dumps(source))
        self.assertEqual(source, result)
        self.assertEqual(2, len(sent))
        self.assertEqual(system, sent[1]['messages'][0]['content'])
        self.assertIn('preserve the complete text in its original order', sent[1]['messages'][1]['content'])
        self.assertEqual(['quality_rejected', 'accepted'], [a['outcome'] for a in attempts])
        self.assertEqual(['review_text_mismatch'], attempts[0]['failure_codes'])

    def test_rejected_answer_reaches_the_log_line_but_not_the_prompt(self):
        validate = lambda entries: validate_attribution(FROZEN, entries)
        with tempfile.TemporaryDirectory() as tmp:
            _, sent, printed = self.run_call(tmp, [SHIFTED, CORRECT], validate, max_retries=1)
        warning = next(line for line in printed.splitlines() if "not assigned" in line)
        self.assertIn('returned=[entry 1: "NARRATOR"]', warning)
        self.assertNotIn("returned=", sent[1][1]["content"])
        self.assertNotIn('"NARRATOR"]', sent[1][1]["content"])

    def test_spoken_not_named_records_the_returned_speaker(self):
        report = validate_attribution(FROZEN, json.loads(SHIFTED))
        finding = next(f for f in report["findings"] if f["code"] == "spoken_not_named")
        self.assertEqual("NARRATOR", finding["returned"])

    def test_run_scoped_request_log_sits_beside_the_run_response_log(self):
        with patch.dict(os.environ, {"ALEXANDRIA_RUN_ID": "run-1"}):
            response = gs.get_response_log_path("llm_responses.log")
            request = gs.get_request_log_path(response)
        self.assertEqual(os.path.dirname(response), os.path.dirname(request))
        self.assertTrue(request.endswith(os.path.join("run-1", "llm_responses.requests.log")))

    def test_request_is_closed_and_lock_released_before_backend_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            handles, held, seen = [], [], []
            native_open, native_lock = open, gs.file_lock

            def opening(*args, **kwargs):
                handle = native_open(*args, **kwargs)
                handles.append(handle)
                return handle

            @contextlib.contextmanager
            def locking(*args, **kwargs):
                with native_lock(*args, **kwargs):
                    held.append(True)
                    try:
                        yield
                    finally:
                        held.pop()

            def handle(request):
                seen.append(json.loads(request.content))
                self.assertTrue(handles, "backend entered before any request append")
                self.assertTrue(all(item.closed for item in handles))
                self.assertEqual([], held)
                [logged] = blocks(os.path.join(tmp, "llm_responses.requests.log"))
                self.assertIn(seen[0]["messages"][1]["content"], logged)
                self.assertIn("scope=prepared_logical_attempt", logged)
                return self.sdk_response()

            with patch.object(gs, "open", opening, create=True), \
                    patch.object(gs, "file_lock", locking):
                result, _, _ = self.run_sdk_call(tmp, handle)
            self.assertEqual(json.loads(CORRECT), result)
            self.assertEqual(1, len(seen))

    def test_sdk_timeout_keeps_prepared_request_without_a_response_block(self):
        seen = []

        def handle(request):
            seen.append(json.loads(request.content))
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        with tempfile.TemporaryDirectory() as tmp:
            result, _, attempts = self.run_sdk_call(tmp, handle)
            self.assertEqual([], result)
            self.assertEqual(1, len(seen))
            self.assertEqual("api_error", attempts[0]["outcome"])
            [logged] = blocks(os.path.join(tmp, "llm_responses.requests.log"))
            self.assertIn(seen[0]["messages"][1]["content"], logged)
            self.assertFalse(Path(tmp, "llm_responses.log").exists())

    def test_api_retry_records_distinct_logical_ids_before_each_send(self):
        seen = []

        def handle(request):
            seen.append(json.loads(request.content))
            if len(seen) == 1:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return self.sdk_response()

        params = gs.LLMGenParams("system text", "", 8192, 0.0, 1, api_retry_limit=1)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.run_sdk_call(tmp, handle, params)
            requests = blocks(os.path.join(tmp, "llm_responses.requests.log"))
            [response] = blocks(os.path.join(tmp, "llm_responses.log"))
        self.assertEqual(json.loads(CORRECT), result)
        self.assertEqual(2, len(seen))
        self.assertEqual(2, len(requests))
        ids = [re.search(r"request=(\w+)", item).group(1) for item in requests]
        self.assertNotEqual(ids[0], ids[1])
        self.assertIn(f"request={ids[1]}", response)

    def test_schema_fallback_is_two_wire_sends_under_one_prepared_id(self):
        seen = []

        def handle(request):
            seen.append(json.loads(request.content))
            if "response_format" in seen[-1]:
                return httpx.Response(400, json={"error": {
                    "message": "response_format is unsupported", "type": "invalid_request_error"}})
            return self.sdk_response()

        params = gs.LLMGenParams("system text", "", 8192, 0.0, 1,
                                 api_retry_limit=0, response_schema={
                                     "name": "entries", "schema": {"type": "array"}})
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.run_sdk_call(tmp, handle, params)
            [request] = blocks(os.path.join(tmp, "llm_responses.requests.log"))
            [response] = blocks(os.path.join(tmp, "llm_responses.log"))
        self.assertEqual(json.loads(CORRECT), result)
        self.assertEqual(2, len(seen))
        self.assertIn("response_format", seen[0])
        self.assertNotIn("response_format", seen[1])
        self.assertEqual(seen[0]["messages"], seen[1]["messages"])
        self.assertIn("scope=prepared_logical_attempt", request)
        self.assertIn(re.search(r"request=\w+", request).group(), response)

    def test_failed_request_append_does_not_suppress_response_or_retry_provider(self):
        seen = []

        def handle(request):
            seen.append(request)
            return self.sdk_response()

        native_open = open

        def opening(path, *args, **kwargs):
            if str(path).endswith(".requests.log"):
                raise OSError(errno.ENOSPC, "synthetic request append refusal")
            return native_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(gs, "open", opening, create=True), \
                patch.object(gs, "classify_llm_error", wraps=gs.classify_llm_error) as classify:
            result, output, _ = self.run_sdk_call(tmp, handle)
            [response] = blocks(os.path.join(tmp, "llm_responses.log"))
        self.assertEqual(json.loads(CORRECT), result)
        self.assertEqual(1, len(seen))
        self.assertIn(CORRECT, response)
        self.assertIn("WARNING: request logging failed", output)
        classify.assert_not_called()

    def test_input_budget_refusal_never_logs_or_sends_a_prepared_request(self):
        seen = []

        def handle(request):
            seen.append(request)
            return self.sdk_response()

        params = gs.LLMGenParams("system text", "", 8192, 0.0, 1, max_input_tokens=1)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, attempts = self.run_sdk_call(tmp, handle, params)
            self.assertEqual([], result)
            self.assertEqual("context_budget", attempts[0]["error_category"])
            self.assertEqual([], seen)
            self.assertFalse(Path(tmp, "llm_responses.requests.log").exists())

    def test_temperature_zero_duplicate_refusal_does_not_append_a_second_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, sent, _ = self.run_call(tmp, ["no JSON"], max_retries=2)
            self.assertEqual([], result)
            self.assertEqual(1, len(sent))
            self.assertEqual(1, len(blocks(os.path.join(tmp, "llm_responses.requests.log"))))


if __name__ == "__main__":
    unittest.main()
