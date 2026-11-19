"""Issue #2079: review prompt presets round-trip and keep their placeholders.

Every prompt in this module is synthetic. Nothing here quotes a published book.
"""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config_settings
from config_settings import AppConfig, PromptConfig, load_app_config_result
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from routers import system


def make_config(**prompts):
    profile = {"base_url": "http://localhost:1/v1", "api_key": "local", "model_name": "fixture"}
    return AppConfig(
        llm=profile, llm_local=profile, llm_mode="local",
        tts={},
        prompts=prompts,
    )


class ReviewPresetModelTests(unittest.TestCase):
    def test_config_accepts_and_round_trips_review_presets(self):
        preset = config_settings.ReviewPromptPreset(
            name="Synthetic review", description="why",
            system_prompt="Synthetic system instructions.",
            user_prompt="Context:\n{context}\nEntries:\n{batch}",
        )
        config = make_config(
            review_preset=preset.name,
            review_prompt_presets=[preset],
            review_system_prompt="Synthetic saved system.",
            review_user_prompt="Context:\n{context}\nEntries:\n{batch}",
        )
        reloaded = AppConfig.model_validate(json.loads(config.model_dump_json()))
        self.assertEqual("Synthetic review", reloaded.prompts.review_preset)
        self.assertEqual([preset], reloaded.prompts.review_prompt_presets)
        self.assertEqual("Synthetic saved system.", reloaded.prompts.review_system_prompt)
        self.assertEqual(preset.user_prompt, reloaded.prompts.review_user_prompt)

    def test_missing_or_unknown_placeholders_are_refused_with_a_clear_message(self):
        bad_templates = {
            "no context": "Entries only: {batch}",
            "no batch": "Context only: {context}",
            "neither": "No payloads at all",
            "unknown": "Context: {context}; batch: {batch}; extra: {other}",
            "traversal": "Context: {context.value}; batch: {batch}",
            "formatting": "Context: {context!r}; batch: {batch}",
            "blank": "   ",
        }
        for label, template in bad_templates.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError) as raised:
                    config_settings.ReviewPromptPreset(
                        name="bad", system_prompt="Synthetic system", user_prompt=template
                    )
                message = str(raised.exception)
                self.assertIn("Review user prompt", message)
                if label == "no context":
                    self.assertIn("{context}", message)
                if label == "no batch":
                    self.assertIn("{batch}", message)

    def test_old_configs_load_unchanged_and_default_to_default(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "config.json")
            path.write_text(json.dumps({
                "llm": {"base_url": "http://localhost:1/v1", "api_key": "local", "model_name": "fixture"},
                "tts": {},
                "prompts": {
                    "review_system_prompt": "Synthetic saved system.",
                    "review_user_prompt": "Context: {context}\nEntries: {batch}",
                },
            }), encoding="utf-8")
            loaded = load_app_config_result(str(path))
            self.assertEqual((), loaded.warnings)
            self.assertNotIn("review_preset", loaded.data["prompts"])
            self.assertNotIn("review_prompt_presets", loaded.data["prompts"])
            self.assertEqual("Synthetic saved system.", loaded.data["prompts"]["review_system_prompt"])
            model = make_config(**loaded.data["prompts"])
            self.assertEqual("default", model.prompts.review_preset)
            self.assertEqual([], model.prompts.review_prompt_presets)
            self.assertEqual("default", PromptConfig().review_preset)
            self.assertEqual([], PromptConfig().review_prompt_presets)


class ReviewPresetApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(system.router)
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name, "config.json")
        self.path.write_text("{}", encoding="utf-8")
        self.patches = [
            patch.object(system, "CONFIG_PATH", str(self.path)),
            patch.object(system, "project_manager", SimpleNamespace(reset_engine=lambda: None, engine=None)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(self.app)

    def test_get_config_returns_review_presets_alongside_other_passes(self):
        self.path.write_text(json.dumps({
            "prompts": {
                "revP": "probe",
                "review_preset": "Synthetic review",
                "review_prompt_presets": [{
                    "name": "Synthetic review", "description": "why",
                    "system_prompt": "Synthetic system.",
                    "user_prompt": "Context: {context}\nEntries: {batch}",
                }],
                "pass1_preset": "default",
                "pass1_prompt_presets": [],
            }
        }), encoding="utf-8")
        response = self.client.get("/api/config")
        self.assertEqual(200, response.status_code, response.text)
        prompts = response.json()["prompts"]
        self.assertEqual("Synthetic review", prompts["review_preset"])
        self.assertEqual("Synthetic review", prompts["review_prompt_presets"][0]["name"])
        self.assertIn("pass1_prompt_presets", prompts)

    def test_save_persists_review_presets_and_refuses_missing_placeholders(self):
        payload = json.loads(make_config(
            review_preset="Synthetic review",
            review_prompt_presets=[config_settings.ReviewPromptPreset(
                name="Synthetic review", system_prompt="Synthetic system.",
                user_prompt="Context: {context}\nEntries: {batch}",
            )],
            review_system_prompt="Synthetic saved system.",
            review_user_prompt="Context: {context}\nEntries: {batch}",
        ).model_dump_json())
        response = self.client.post("/api/config", json=payload)
        self.assertEqual(200, response.status_code, response.text)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual("Synthetic review", saved["prompts"]["review_preset"])
        self.assertEqual("Synthetic system.", saved["prompts"]["review_prompt_presets"][0]["system_prompt"])
        self.assertEqual("Context: {context}\nEntries: {batch}", saved["prompts"]["review_user_prompt"])

        before = self.path.read_bytes()
        payload["prompts"]["review_prompt_presets"][0]["user_prompt"] = "Entries: {batch}"
        refused = self.client.post("/api/config", json=payload)
        self.assertEqual(422, refused.status_code, refused.text)
        self.assertIn("{context}", refused.text)
        self.assertEqual(before, self.path.read_bytes(), "a refused preset must not be saved")

    def test_default_review_prompts_are_the_checked_in_ones(self):
        response = self.client.get("/api/default_prompts")
        self.assertEqual(200, response.status_code, response.text)
        defaults = response.json()
        self.assertEqual(REVIEW_SYSTEM_PROMPT, defaults["review_system_prompt"])
        self.assertEqual(REVIEW_USER_PROMPT, defaults["review_user_prompt"])
        self.assertIn("{context}", REVIEW_USER_PROMPT)
        self.assertIn("{batch}", REVIEW_USER_PROMPT)


if __name__ == "__main__":
    unittest.main()
