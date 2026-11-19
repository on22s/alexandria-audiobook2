"""Issue #2093: the active review user prompt is checked at save and at review start.

Every prompt in this module is synthetic. Nothing here quotes a published book.
"""
import io
import json
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config_settings
import review_script
from routers import system


VALID = "Context:\n{context}\nEntries:\n{batch}"


def post_payload(review_user_prompt=None, include=True):
    """Build a raw POST body: never via AppConfig, which is the boundary under test."""
    profile = {"base_url": "http://localhost:1/v1", "api_key": "local", "model_name": "fixture"}
    prompts = {"review_system_prompt": "Synthetic saved system."}
    if include:
        prompts["review_user_prompt"] = review_user_prompt
    return {"llm": profile, "llm_local": profile, "llm_mode": "local",
            "tts": {}, "prompts": prompts}


class ReachedClient(RuntimeError):
    """Sentinel: review got as far as creating the LLM client."""


class ActiveReviewPromptApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(system.router)
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name, "config.json")
        self.path.write_text("{}", encoding="utf-8")
        for item in (patch.object(system, "CONFIG_PATH", str(self.path)),
                     patch.object(system, "project_manager",
                                  Mock(reset_engine=lambda: None, engine=None))):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(self.app)

    def test_missing_placeholders_are_refused_and_nothing_is_written(self):
        cases = (
            ("Entries only: {batch}", "{context}"),
            ("Context only: {context}", "{batch}"),
            ("No payloads at all", "{context}"),
        )
        for prompt, missing in cases:
            with self.subTest(prompt=prompt):
                before = self.path.read_bytes()
                response = self.client.post("/api/config", json=post_payload(prompt))
                self.assertEqual(422, response.status_code, response.text)
                self.assertIn("Review user prompt", response.text)
                self.assertIn(missing, response.text)
                self.assertEqual(before, self.path.read_bytes(), "a refused prompt must not be saved")

    def test_none_empty_and_valid_prompts_are_accepted(self):
        for prompt in (None, "", VALID):
            with self.subTest(prompt=prompt):
                response = self.client.post("/api/config", json=post_payload(prompt))
                self.assertEqual(200, response.status_code, response.text)
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(prompt, saved["prompts"]["review_user_prompt"])

    def test_omitted_active_prompt_is_still_valid(self):
        response = self.client.post("/api/config", json=post_payload(include=False))
        self.assertEqual(200, response.status_code, response.text)

    def test_existing_invalid_prompt_still_loads(self):
        bad = "Synthetic saved text without payloads"
        self.path.write_text(json.dumps(post_payload(bad)), encoding="utf-8")
        loaded = config_settings.load_app_config_result(str(self.path))
        self.assertEqual((), loaded.warnings)
        self.assertEqual(bad, loaded.data["prompts"]["review_user_prompt"])
        self.assertEqual(
            bad, config_settings.load_app_config(str(self.path))["prompts"]["review_user_prompt"])
        response = self.client.get("/api/config")
        self.assertEqual(200, response.status_code, response.text)


class ActiveReviewPromptStartTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.dir = Path(self.root.name)
        self.source = self.dir / "annotated_script.json"
        self.source.write_text(json.dumps([
            {"speaker": "ALICE", "text": "Synthetic line for the fixture.", "instruct": "calm"}
        ]), encoding="utf-8")
        self.config = self.dir / "config.json"

    def run_main(self, prompt, extra_args=()):
        self.config.write_text(json.dumps({
            "llm_mode": "local",
            "llm_local": {"base_url": "http://localhost:1/v1", "model_name": "fixture",
                          "api_key": "local"},
            "prompts": {"review_user_prompt": prompt},
        }), encoding="utf-8")
        argv = ["review_script.py", "--input", str(self.source), *extra_args]
        with ExitStack() as stack:
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(review_script.sys, "argv", argv))
            stack.enter_context(patch.object(review_script, "get_runtime_data_dir",
                                             return_value=str(self.dir)))
            stack.enter_context(patch.object(review_script, "get_app_config_path",
                                             return_value=str(self.config)))
            stack.enter_context(patch.object(review_script, "ensure_ideal_settings",
                                             return_value=(False, {}, "CPU fixture settings")))
            client = stack.enter_context(patch.object(
                review_script, "make_run_client", side_effect=ReachedClient("reached client creation")))
            try:
                review_script.main()
                failure = None
            except BaseException as exc:
                failure = exc
            return output.getvalue(), failure, client

    def test_invalid_active_prompt_stops_before_client_creation(self):
        for prompt, missing in (("Entries only: {batch}", "{context}"),
                                ("Context only: {context}", "{batch}"),
                                ("No payloads at all", "{context}")):
            with self.subTest(prompt=prompt):
                output, failure, client = self.run_main(prompt)
                self.assertIsInstance(failure, ValueError, output)
                first_line = str(failure).splitlines()[0]
                self.assertIn(f"missing {missing}", first_line)
                self.assertIn("Setup > Review prompts", first_line)
                self.assertIn("restore both {context} and {batch}", first_line)
                self.assertIn("clear it to use the checked-in default", first_line)
                self.assertIn("Review user prompt", str(failure))
                client.assert_not_called()

    def test_default_and_valid_prompts_reach_client_creation(self):
        for prompt in (VALID, None, ""):
            with self.subTest(prompt=prompt):
                output, failure, client = self.run_main(prompt)
                self.assertIsInstance(failure, ReachedClient, output)
                client.assert_called_once()

    def test_labels_only_ignores_the_active_prompt(self):
        output, failure, client = self.run_main("no placeholders here",
                                                extra_args=("--labels-only",))
        self.assertIsInstance(failure, ReachedClient, output)
        client.assert_called_once()


if __name__ == "__main__":
    unittest.main()
