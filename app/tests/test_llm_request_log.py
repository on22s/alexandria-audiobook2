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
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
