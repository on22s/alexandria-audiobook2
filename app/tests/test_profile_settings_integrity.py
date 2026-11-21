"""Focused regressions for LLM profile completeness and Setup-save cache retention."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import llm_bench
import routers.system as system_module
from config_settings import AppConfig
from lmstudio_settings import get_active_llm_config, get_failover_llm_config, get_saved_llm_profile

LEGACY = {"base_url": "http://localhost:1234/v1", "api_key": "fixture", "model_name": "legacy-model"}
ACTIVE = {"base_url": "http://127.0.0.1:5555/v1", "api_key": "fixture", "model_name": "active-model"}
MANUAL = {"transport": "manual", "api_key": "fixture"}
CACHE_FOR = "http://127.0.0.1:5555/v1::active-model"


def _find(value):
    def find(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
        if on_measurement is not None:
            on_measurement(1, 10.0, 0.01)
        return value
    return find


class ProfileCompletenessTests(unittest.TestCase):
    def test_partial_primary_falls_back_to_a_complete_legacy_profile(self):
        partial = {"api_key": "fixture"}
        config = {"llm_mode": "local", "llm": LEGACY, "llm_local": partial}
        self.assertEqual(LEGACY, get_active_llm_config(config))
        self.assertEqual(partial, get_saved_llm_profile(config),
                         "credential lookup still sees the raw saved profile")

    def test_a_complete_primary_still_wins_over_the_legacy_mirror(self):
        config = {"llm_mode": "local", "llm": LEGACY, "llm_local": ACTIVE}
        self.assertEqual(ACTIVE, get_active_llm_config(config))
        self.assertEqual(ACTIVE, get_saved_llm_profile(config))

    def test_manual_transport_is_complete_without_an_endpoint(self):
        config = {"llm_mode": "local", "llm": LEGACY, "llm_local": MANUAL}
        self.assertEqual(MANUAL, get_active_llm_config(config))
        self.assertEqual(MANUAL, get_saved_llm_profile(config))

    def test_failover_uses_the_same_completeness_policy(self):
        config = {"llm_failover": True, "llm_mode": "local", "llm_local": ACTIVE,
                  "llm_remote": {"api_key": "fixture"}}
        self.assertEqual({}, get_failover_llm_config(config))
        config["llm_remote"] = MANUAL
        self.assertEqual(MANUAL, get_failover_llm_config(config))


class SetupSaveConcurrencyCacheTests(unittest.TestCase):
    def _cache(self, environment):
        return {"concurrency": 6, "concurrency_for": CACHE_FOR,
                "concurrency_environment": environment, "concurrency_revision": "fixture-revision"}

    def _seed(self, tmp, profile, cache):
        document = {"llm_mode": "local", "llm": dict(profile, **cache),
                    "llm_local": dict(profile, **cache), "tts": {"mode": "local"},
                    "generation": {}}
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path

    def _payload(self, profile):
        return {"llm_mode": "local", "llm": dict(profile), "llm_local": dict(profile),
                "tts": {"mode": "local", "pause_same_speaker_ms": 321}, "generation": {}}

    def _save(self, tmp, payload):
        path = os.path.join(tmp, "config.json")
        with patch.object(system_module, "CONFIG_PATH", path), \
             patch.object(system_module.project_manager, "invalidate_config_cache"), \
             patch.object(system_module.project_manager, "engine", None):
            asyncio.run(system_module.save_config(AppConfig.model_validate(payload)))
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_unrelated_setup_save_keeps_the_measured_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, ACTIVE, self._cache({"version": 2, "base_url": ACTIVE["base_url"]}))
            payload = self._payload(dict(ACTIVE, api_key=system_module._REDACTED_SECRET))
            saved = self._save(tmp, payload)
            for section in ("llm", "llm_local"):
                for key, value in self._cache({"version": 2, "base_url": ACTIVE["base_url"]}).items():
                    self.assertEqual(value, saved[section][key], section)
                self.assertEqual("fixture", saved[section]["api_key"])
            self.assertEqual(321, saved["tts"]["pause_same_speaker_ms"])

    def test_changed_endpoint_or_model_drops_the_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, ACTIVE, self._cache({"version": 2, "base_url": ACTIVE["base_url"]}))
            changed = {"base_url": "http://127.0.0.1:6666/v1", "api_key": "fixture-rotated",
                       "model_name": "active-model"}
            saved = self._save(tmp, self._payload(changed))
            for section in ("llm", "llm_local"):
                for key in self._cache({}):
                    self.assertNotIn(key, saved[section])
            changed["base_url"] = ACTIVE["base_url"]
            changed["model_name"] = "other-model"
            saved = self._save(tmp, self._payload(changed))
            for section in ("llm", "llm_local"):
                for key in self._cache({}):
                    self.assertNotIn(key, saved[section])

    def test_preserved_cache_is_reused_but_a_changed_endpoint_rebenchmarks(self):
        status = {"available": True, "loaded": True, "parallel": 8, "context_length": 8192,
                  "runtime": "llama.cpp"}
        environment = llm_bench.get_concurrency_environment(
            ACTIVE["base_url"], ACTIVE["model_name"], False, None,
            "fixture-host", "fixture-gpu", "fixture-backend", status)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._seed(tmp, ACTIVE, self._cache(environment))
            self._save(tmp, self._payload(dict(ACTIVE, api_key=system_module._REDACTED_SECRET)))
            with patch.object(llm_bench.platform, "node", return_value="fixture-host"), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-gpu", "fixture-backend")), \
                 patch.object(llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                reused = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", ACTIVE["base_url"], ACTIVE["model_name"], object(), status=status)
                self.assertEqual(6, reused)
                bench.assert_not_called()

            changed = {"base_url": "http://127.0.0.1:6666/v1", "api_key": "fixture-rotated",
                       "model_name": "active-model"}
            self._save(tmp, self._payload(changed))
            with patch.object(llm_bench.platform, "node", return_value="fixture-host"), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-gpu", "fixture-backend")), \
                 patch.object(llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                fresh = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", changed["base_url"], changed["model_name"], object(), status=status)
                self.assertEqual(3, fresh)
                bench.assert_called_once()


if __name__ == "__main__":
    unittest.main()
