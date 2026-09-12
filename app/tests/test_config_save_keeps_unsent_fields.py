"""A Setup-tab save must not reset settings the tab does not render."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import routers.system as system_module
from config_settings import AppConfig


def _saved_config(tmp, generation, tts=None):
    profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
    doc = {"llm": profile, "llm_mode": "local", "llm_local": profile,
           "tts": tts or {"mode": "local"}, "generation": generation}
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


class KeepUnsentFieldsTests(unittest.TestCase):
    def _save(self, tmp, payload):
        path = os.path.join(tmp, "config.json")
        with patch.object(system_module, "CONFIG_PATH", path), \
             patch.object(system_module.project_manager, "invalidate_config_cache"), \
             patch.object(system_module.project_manager, "engine", None):
            asyncio.run(system_module.save_config(AppConfig.model_validate(payload)))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_hand_set_three_pass_and_rescue_values_survive_a_save_that_omits_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {"chunk_size": 3000, "three_pass_chunk_size": 9000,
                                "context_rescue_windows": [1500], "context_rescue_retries": 5},
                          tts={"mode": "local", "external_timeout_seconds": 42})
            profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
            # What an older Setup tab posts: generation with only the fields it shows.
            saved = self._save(tmp, {"llm": profile, "llm_mode": "local", "llm_local": profile,
                                     "tts": {"mode": "local"},
                                     "generation": {"chunk_size": 2500, "temperature": 0.4}})
            self.assertEqual(2500, saved["generation"]["chunk_size"], "sent fields are applied")
            self.assertEqual(0.4, saved["generation"]["temperature"])
            self.assertEqual(9000, saved["generation"]["three_pass_chunk_size"], "unsent field kept")
            self.assertEqual([1500], saved["generation"]["context_rescue_windows"])
            self.assertEqual(5, saved["generation"]["context_rescue_retries"])
            self.assertEqual(42, saved["tts"]["external_timeout_seconds"])

    def test_a_field_sent_at_its_default_value_is_applied_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {"three_pass_chunk_size": 9000})
            profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
            saved = self._save(tmp, {"llm": profile, "llm_mode": "local", "llm_local": profile,
                                     "tts": {"mode": "local"},
                                     "generation": {"three_pass_chunk_size": 3000}})
            self.assertEqual(3000, saved["generation"]["three_pass_chunk_size"],
                             "the client sent the default on purpose; the request decides, not equality with the default")

    def test_keep_unsent_fields_ignores_unknown_saved_keys(self):
        profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
        cfg = AppConfig.model_validate({"llm": profile, "llm_mode": "local", "llm_local": profile,
                                        "tts": {"mode": "local"}, "generation": {"chunk_size": 3000}})
        merged = system_module.keep_unsent_fields(cfg, {"generation": {"not_a_field": 1, "top_k": 33}})
        self.assertEqual(33, merged.generation.top_k)
        self.assertFalse(hasattr(merged.generation, "not_a_field"))


if __name__ == "__main__":
    unittest.main()
