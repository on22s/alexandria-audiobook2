"""Manual transport (#593): the user is the model. The client hands each
request to the data dir and waits; the endpoints are how the reply comes
back; the pipeline's own validation gates it."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
import llm_provider
from llm_provider import ManualClient, make_llm_client
from routers import script


def _answer_when_pending(client, content, mismatch_first=False):
    """A thread standing in for the user: waits for pending.json, replies."""
    def run():
        for _ in range(200):
            if os.path.exists(client.pending_path):
                break
            time.sleep(0.02)
        with open(client.pending_path, encoding="utf-8") as handle:
            req = json.load(handle)
        if mismatch_first:
            with open(client.response_path, "w") as handle:
                json.dump({"id": "not-this-one", "content": "stale"}, handle)
            time.sleep(0.1)
        with open(client.response_path, "w") as handle:
            json.dump({"id": req["id"], "content": content}, handle)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class ManualClientTests(unittest.TestCase):
    def test_non_object_replies_keep_request_pending_until_valid_reply(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            bad_replies = [None, "bad", ["bad"]]
            pending_id = None
            polls = 0

            def publish_response(_seconds):
                nonlocal pending_id, polls
                polls += 1
                self.assertLessEqual(polls, 4, "client never accepted the valid response")
                self.assertFalse(os.path.exists(client.response_path))
                with open(client.pending_path) as handle:
                    pending = json.load(handle)
                if pending_id is None:
                    pending_id = pending["id"]
                self.assertEqual(pending_id, pending["id"])
                reply = bad_replies.pop(0) if bad_replies else {"id": pending_id, "content": "valid"}
                with open(client.response_path, "w") as handle:
                    json.dump(reply, handle)

            output = io.StringIO()
            with patch.object(llm_provider.time, "sleep", side_effect=publish_response), \
                 redirect_stdout(output):
                response = client.create(model="test", messages=[])
            self.assertEqual("valid", response.choices[0].message.content)
            self.assertEqual(4, polls)
            self.assertEqual(3, output.getvalue().count("JSON object"))
            self.assertEqual(1, client.sequence)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))


    def test_dead_request_owner_is_replaced_when_a_new_request_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            client.POLL_SECONDS = 0.01
            os.makedirs(client.dir, exist_ok=True)
            with open(client.pending_path, "w", encoding="utf-8") as target:
                json.dump({"id": "abandoned", "owner_pid": 99999999,
                           "owner_thread": 1}, target)
            with open(client.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": "abandoned", "content": "stale"}, target)
            def answer_new_request():
                for _ in range(200):
                    try:
                        with open(client.pending_path, encoding="utf-8") as source:
                            pending = json.load(source)
                        if pending["id"] != "abandoned":
                            with open(client.response_path, "w", encoding="utf-8") as target:
                                json.dump({"id": pending["id"], "content": "fresh"}, target)
                            return
                    except (FileNotFoundError, ValueError):
                        pass
                    time.sleep(0.01)
            threading.Thread(target=answer_new_request, daemon=True).start()
            answer = client.create(model="m", messages=[])
            self.assertEqual("fresh", answer.choices[0].message.content)
            self.assertFalse(os.path.exists(client.pending_path))

    def test_second_client_waits_without_erasing_first_pending_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ManualClient(tmp)
            first.POLL_SECONDS = 0.01
            answers = {}
            one = threading.Thread(target=lambda: answers.update(
                first=first.create(model="m", messages=[{"role": "user", "content": "first"}])
                .choices[0].message.content), daemon=True)
            one.start()
            for _ in range(200):
                if os.path.exists(first.pending_path):
                    break
                time.sleep(0.01)
            with open(first.pending_path, encoding="utf-8") as source:
                first_request = json.load(source)

            with patch("utils.get_runtime_data_dir", return_value=tmp):
                second = make_llm_client({"transport": "manual"}, 10)
            second.POLL_SECONDS = 0.01
            with open(first.pending_path, encoding="utf-8") as source:
                self.assertEqual(first_request, json.load(source))

            two = threading.Thread(target=lambda: answers.update(
                second=second.create(model="m", messages=[{"role": "user", "content": "second"}])
                .choices[0].message.content), daemon=True)
            two.start()
            time.sleep(0.05)
            with open(first.pending_path, encoding="utf-8") as source:
                self.assertEqual(first_request["id"], json.load(source)["id"])
            self.assertTrue(two.is_alive())

            with open(first.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": first_request["id"], "content": "one"}, target)
            one.join(2)
            self.assertFalse(one.is_alive())
            second_request = None
            for _ in range(200):
                try:
                    with open(second.pending_path, encoding="utf-8") as source:
                        pending = json.load(source)
                    if pending["id"] != first_request["id"]:
                        second_request = pending
                        break
                except FileNotFoundError:
                    pass
                time.sleep(0.01)
            self.assertIsNotNone(second_request)
            with open(second.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": second_request["id"], "content": "two"}, target)
            two.join(2)
            self.assertFalse(two.is_alive())
            self.assertEqual({"first": "one", "second": "two"}, answers)

    def test_request_is_written_reply_is_returned_and_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            _answer_when_pending(client, '[{"n": 0, "speaker": "ELENA"}]')
            response = client.chat.completions.create(
                model="m", messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                temperature=0, max_tokens=500, response_format={"type": "json_schema"})
            self.assertEqual('[{"n": 0, "speaker": "ELENA"}]', response.choices[0].message.content)
            self.assertEqual("stop", response.choices[0].finish_reason)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))

    def test_the_request_file_carries_the_exact_messages_and_a_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            seen = {}

            def run():
                for _ in range(200):
                    if os.path.exists(client.pending_path):
                        break
                    time.sleep(0.02)
                with open(client.pending_path, encoding="utf-8") as handle:
                    seen.update(json.load(handle))
                with open(client.response_path, "w") as handle:
                    json.dump({"id": seen["id"], "content": "ok"}, handle)
            threading.Thread(target=run, daemon=True).start()
            client.create(model="m", messages=[{"role": "user", "content": "hello"}], temperature=0.1)
            self.assertEqual([{"role": "user", "content": "hello"}], seen["messages"])
            self.assertEqual(1, seen["sequence"])
            self.assertEqual(0.1, seen["params"]["temperature"])
            self.assertFalse(seen["params"]["json_schema"])
            _answer_when_pending(client, "two")
            client.create(model="m", messages=[])
            self.assertEqual(2, client.sequence)

    def test_pending_artifact_preserves_completion_contract_and_schema(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            schema = {"type": "json_schema", "json_schema": {
                "name": "annotation", "strict": True, "schema": {
                    "type": "object", "properties": {"speaker": {"type": "string"}},
                    "required": ["speaker"], "additionalProperties": False}}}
            kwargs = {"model": "o3", "messages": [{"role": "user", "content": "hello"}],
                      "max_completion_tokens": 512, "response_format": schema,
                      "top_p": 0.8, "seed": 123,
                      "extra_body": {"reasoning_effort": "high", "top_k": 20},
                      "timeout": object(), "extra_headers": {"Authorization": "secret"}}
            original = copy.deepcopy({k: v for k, v in kwargs.items() if k != "timeout"})
            seen = {}

            def answer(_seconds):
                self.assertFalse(seen, "request did not accept the matching reply")
                with open(client.pending_path, encoding="utf-8") as handle:
                    seen.update(json.load(handle))
                with open(client.response_path, "w", encoding="utf-8") as handle:
                    json.dump({"id": seen["id"], "content": '{"speaker":"NARRATOR"}'}, handle)

            with patch.object(llm_provider.time, "sleep", side_effect=answer):
                result = client.create(**kwargs)
            for key in ("max_completion_tokens", "response_format", "top_p", "seed", "extra_body"):
                self.assertEqual(kwargs[key], seen["params"][key])
            self.assertTrue(seen["params"]["json_schema"])
            self.assertNotIn("timeout", seen["params"])
            self.assertNotIn("extra_headers", seen["params"])
            self.assertNotIn("messages", seen["params"])
            self.assertEqual(kwargs["messages"], seen["messages"])
            self.assertEqual('{"speaker":"NARRATOR"}', result.choices[0].message.content)
            self.assertEqual(original, {k: v for k, v in kwargs.items() if k != "timeout"})
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))

    def test_a_reply_to_another_request_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            _answer_when_pending(client, "right", mismatch_first=True)
            response = client.create(model="m", messages=[])
            self.assertEqual("right", response.choices[0].message.content)

    def test_dispatch_returns_the_manual_client_only_for_that_transport(self):
        self.assertIsInstance(make_llm_client({"transport": "manual", "base_url": "http://x/v1",
                                               "api_key": "k", "model_name": "m"}, 10), ManualClient)
        self.assertIsInstance(make_llm_client({"base_url": "http://localhost:1234/v1",
                                               "api_key": "k", "model_name": "m"}, 10),
                              llm_provider.ConfiguredOpenAI)
        manual = make_llm_client({"transport": "manual"}, 10)
        self.assertIs(manual, manual.with_options(timeout=5))

    def test_a_manual_profile_is_never_on_this_gpu(self):
        config = {"llm_mode": "local", "llm": {"base_url": "http://localhost:1234/v1", "api_key": "k",
                                                 "model_name": "m", "transport": "manual"}}
        with patch.object(core, "load_app_config", return_value=config):
            self.assertFalse(core.llm_is_on_this_gpu())
        config["llm"]["transport"] = "http"
        with patch.object(core, "load_app_config", return_value=config):
            self.assertTrue(core.llm_is_on_this_gpu())


class ManualThreePassTests(unittest.TestCase):
    def test_a_three_pass_run_completes_with_a_person_answering_every_request(self):
        """Run the native file-reply fixture in an owned, bounded CPU child."""
        code = (
            "import faulthandler; faulthandler.dump_traceback_later(8, exit=True); "
            "from tests.test_manual_llm import ManualThreePassTests; "
            "ManualThreePassTests()._run_manual_three_pass_fixture()"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(os.path.abspath(path) for path in sys.path)}
        try:
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            self.fail(f"Manual three-pass fixture timed out: {error.stdout!r} {error.stderr!r}")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def _run_manual_three_pass_fixture(self):
        """No HTTP: each request lands in the data dir, a stand-in user replies,
        the pipeline validates and moves on. Same three answers as the fake
        client fixtures elsewhere, delivered through the files."""
        import three_pass_generate as tp
        from generate_script import LLMGenParams
        source = "Elena entered the cold room. \"Tell me the truth.\""
        answers = [
            json.dumps([{"type": "NARRATOR", "text": "Elena entered the cold room."},
                        {"type": "SPOKEN", "text": "Tell me the truth."}]),
            json.dumps([{"n": 0, "head": "Elena entered the", "speaker": "NARRATOR"},
                        {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]),
            json.dumps([{"n": 0, "head": "Elena entered the", "instruct": "Cold, still narration."},
                        {"n": 1, "head": "Tell me the", "instruct": "Firm, quiet demand."}]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            sequences = []

            def person():
                answered = None
                for content in answers:
                    for _ in range(500):   # wait for a request we have not answered yet
                        try:
                            with open(client.pending_path, encoding="utf-8") as handle:
                                req = json.load(handle)
                        except (FileNotFoundError, ValueError):
                            req = None
                        if req and req["id"] != answered:
                            break
                        time.sleep(0.02)
                    sequences.append(req["sequence"])
                    answered = req["id"]
                    with open(client.response_path, "w") as handle:
                        json.dump({"id": req["id"], "content": content}, handle)
            threading.Thread(target=person, daemon=True).start()
            entries = tp.run_three_pass(client, "m", source,
                                        LLMGenParams(max_tokens=500, temperature=0.1, segmentation="llm"),
                                        chunk_size=6000)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))
        self.assertEqual(["Elena entered the cold room.", "Tell me the truth."],
                         [entry["text"] for entry in entries])
        self.assertTrue(all(entry.get("instruct") for entry in entries))
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in entries])
        self.assertEqual([1, 2, 3], sequences)


class ManualEndpointTests(unittest.TestCase):
    def test_pending_and_response_round_trip_with_stage_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            state = script.process_state["script"]
            original = dict(state)
            try:
                state.update({"running": True, "logs": [
                    "Step 2 (speakers): window 2 of 4 - asking the model",
                    "Warning: ATTRIBUTE failed quality validation (attempt 1): expected 4 entries, got 2",
                    "Retrying... (attempt 2 of 4)"]})
                with patch.object(script, "DATA_DIR", tmp):
                    self.assertIsNone(asyncio.run(script.manual_llm_pending())["pending"])
                    with self.assertRaises(script.HTTPException) as ctx:
                        asyncio.run(script.manual_llm_response(script.ManualReply(id="x", content="y")))
                    self.assertEqual(409, ctx.exception.status_code)
                    done = {}
                    t = threading.Thread(target=lambda: done.update(
                        text=client.create(model="m", messages=[{"role": "user", "content": "Q"}])
                        .choices[0].message.content), daemon=True)
                    t.start()
                    for _ in range(200):
                        if os.path.exists(client.pending_path):
                            break
                        time.sleep(0.02)
                    status = asyncio.run(script.get_status("script"))
                    self.assertEqual(1, status["manual_request"]["sequence"])
                    self.assertIn("Retrying... (attempt 2 of 4)", status["manual_request"]["stage_hint"])
                    self.assertIn("Step 2 (speakers): window 2 of 4", status["manual_request"]["stage_hint"])
                    pending = asyncio.run(script.manual_llm_pending())["pending"]
                    self.assertEqual([{"role": "user", "content": "Q"}], pending["messages"])
                    with self.assertRaises(script.HTTPException) as ctx:
                        asyncio.run(script.manual_llm_response(script.ManualReply(id="wrong", content="y")))
                    self.assertEqual(409, ctx.exception.status_code)
                    reply = asyncio.run(script.manual_llm_response(
                        script.ManualReply(id=pending["id"], content="A")))
                    self.assertTrue(reply["accepted"])
                    t.join(5)
                    self.assertEqual("A", done.get("text"))
                    state["running"] = False
                    self.assertIsNone(asyncio.run(script.get_status("script"))["manual_request"])
            finally:
                state.clear()
                state.update(original)


if __name__ == "__main__":
    unittest.main()
