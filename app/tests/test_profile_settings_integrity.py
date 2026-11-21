"""Focused regressions for LLM profile completeness and Setup-save cache retention."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import llm_bench
import llm_provider
import routers.system as system_module
from config_settings import AppConfig, LLMConfig
from lmstudio_settings import (get_active_llm_config, get_failover_llm_config,
                               get_saved_llm_profile, is_profile_configured)

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


HOST = "fixture-host"
GPU = "fixture-gpu"
BACKEND = "fixture-backend"


def _seed_config(tmp, document):
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def _load_config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save_payload(tmp, payload):
    path = os.path.join(tmp, "config.json")
    with patch.object(system_module, "CONFIG_PATH", path), \
         patch.object(system_module.project_manager, "invalidate_config_cache"), \
         patch.object(system_module.project_manager, "engine", None):
        asyncio.run(system_module.save_config(AppConfig.model_validate(payload)))
    return _load_config(path)


def _get_document(tmp):
    path = os.path.join(tmp, "config.json")
    with patch.object(system_module, "CONFIG_PATH", path):
        return asyncio.run(system_module.get_config())


def _benchmark_status(parallel=8):
    return {"available": True, "loaded": True, "parallel": parallel, "context_length": 8192,
            "runtime": "lmstudio"}


def _benchmark_environment(status, base_url=ACTIVE["base_url"], model=ACTIVE["model_name"],
                           is_remote=False, ssh_alias=None):
    return llm_bench.get_concurrency_environment(base_url, model, is_remote, ssh_alias,
                                                 HOST, GPU, BACKEND, status)


def _cache_entry(environment=None, base_url=ACTIVE["base_url"], model=ACTIVE["model_name"]):
    return {"concurrency": 5, "concurrency_for": f"{base_url}::{model}",
            "concurrency_environment": environment if environment is not None else {},
            "concurrency_revision": "fixture-revision"}


def _patch_benchmark():
    return (patch.object(llm_bench.platform, "node", return_value=HOST),
            patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                         return_value=(GPU, BACKEND)))


class ProfileSelectionPredicateTests(unittest.TestCase):
    def test_blank_default_and_cache_only_inactive_blocks_are_not_failover(self):
        blank = LLMConfig(base_url="", api_key="", model_name="").model_dump()
        for inactive in (blank, _cache_entry(), {"transport": "http"},
                         {"api_key": "fixture"}, {"concurrency": 6}):
            config = {"llm_mode": "local", "llm_failover": True, "llm": LEGACY,
                      "llm_local": ACTIVE, "llm_remote": inactive}
            self.assertFalse(is_profile_configured(inactive), inactive)
            self.assertEqual({}, get_failover_llm_config(config), inactive)

    def test_partial_and_manual_primaries_remain_configured(self):
        for partial in ({"base_url": ACTIVE["base_url"]}, {"model_name": ACTIVE["model_name"]}, MANUAL):
            self.assertTrue(is_profile_configured(partial), partial)
            config = {"llm_mode": "local", "llm": LEGACY, "llm_local": partial}
            self.assertEqual(partial, get_active_llm_config(config))

    def test_blank_inactive_setup_payload_creates_primary_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_config(tmp, {"llm_mode": "local", "llm": LEGACY, "llm_local": LEGACY,
                               "tts": {"mode": "local"}, "generation": {}})
            document = _get_document(tmp)
            document["llm_remote"] = LLMConfig(base_url="", api_key="", model_name="").model_dump()
            saved = _save_payload(tmp, document)
            self.assertEqual({}, get_failover_llm_config(saved))
            self.assertFalse(_get_document(tmp)["failover_is_remote"])
            self.assertEqual(LEGACY["base_url"], get_active_llm_config(saved)["base_url"])


class BenchmarkWriterSelectionTests(unittest.TestCase):
    def test_cache_only_named_slot_does_not_shadow_legacy_endpoint(self):
        blank = LLMConfig(base_url="", api_key="", model_name="").model_dump()
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed_config(tmp, {"llm_mode": "local", "llm": LEGACY, "llm_local": blank,
                                      "tts": {"mode": "local"}, "generation": {}})
            patches = _patch_benchmark()
            with patches[0], patches[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", LEGACY["base_url"], LEGACY["model_name"], object(),
                    status=_benchmark_status())
            bench.assert_called_once()
            self.assertEqual(3, value)
            saved = _load_config(path)
            self.assertEqual(3, saved["llm_local"]["concurrency"])
            self.assertEqual(LEGACY["base_url"], get_active_llm_config(saved)["base_url"])
            self.assertEqual({}, get_failover_llm_config({**saved, "llm_failover": True}))
            with patch.object(llm_provider, "make_llm_client") as make:
                make.return_value = sentinel = object()
                client = llm_provider.make_run_client(
                    {**saved, "llm_failover": True}, get_active_llm_config(saved), 30)
                self.assertIs(sentinel, client)
                self.assertEqual(1, make.call_count)
                self.assertEqual(LEGACY["base_url"], make.call_args_list[0].args[0]["base_url"])


class LegacyCacheMigrationTests(unittest.TestCase):
    def test_actual_get_then_unrelated_save_migrates_evidence_to_named_slot(self):
        status = _benchmark_status()
        entry = _cache_entry(_benchmark_environment(status))
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed_config(tmp, {"llm_mode": "local", "llm": dict(ACTIVE, **entry),
                                      "llm_local": dict(ACTIVE), "tts": {"mode": "local"},
                                      "generation": {}})
            document = _get_document(tmp)
            document["tts"]["pause_same_speaker_ms"] = 404
            saved = _save_payload(tmp, document)
            self.assertEqual(404, saved["tts"]["pause_same_speaker_ms"])
            for key, value in entry.items():
                self.assertEqual(value, saved["llm_local"][key], key)
            patches = _patch_benchmark()
            with patches[0], patches[1], patch.object(
                    llm_bench, "find_optimal_concurrency",
                    side_effect=AssertionError("cache hit must not sweep")) as bench:
                reused = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", ACTIVE["base_url"], ACTIVE["model_name"], object(), status=status)
            bench.assert_not_called()
            self.assertEqual(entry["concurrency"], reused)


class BenchmarkIdentityTests(unittest.TestCase):
    def _seeded(self, tmp, profile=ACTIVE, ssh_alias=None, mode="local", with_cache=True):
        status = _benchmark_status()
        environment = _benchmark_environment(status, profile["base_url"], profile["model_name"],
                                             mode == "remote", ssh_alias)
        entry = _cache_entry(environment, profile["base_url"], profile["model_name"])
        document = {"llm_mode": mode, "llm": dict(profile), f"llm_{mode}": dict(profile),
                    "tts": {"mode": "local"}, "generation": {}}
        if with_cache:
            document["llm"].update(entry)
            document[f"llm_{mode}"].update(entry)
        if ssh_alias is not None:
            document["llm_remote_ssh"] = ssh_alias
        return _seed_config(tmp, document), entry

    def _save_change(self, tmp, section, **changes):
        document = _get_document(tmp)
        document[section].update(changes)
        if "base_url" in changes:
            document[section]["api_key"] = "fixture-rotated"
        return _save_payload(tmp, document)

    def test_independent_endpoint_model_and_request_setting_changes_drop_cache(self):
        changes = (
            {"base_url": "http://127.0.0.1:6666/v1"},
            {"model_name": "other-model"},
            {"request_interval_seconds": 0.25},
            {"reasoning_effort": "high"},
            {"structured_output": "off"},
            {"transport": "manual"},
        )
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                self._seeded(tmp)
                saved = self._save_change(tmp, "llm_local", **change)
                for section in ("llm", "llm_local"):
                    for key in ("concurrency", "concurrency_for", "concurrency_environment", "concurrency_revision"):
                        self.assertNotIn(key, saved[section], (section, key, change))

    def test_remote_ssh_alias_change_drops_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seeded(tmp, ssh_alias="alias-a", mode="remote")
            document = _get_document(tmp)
            document["llm_remote_ssh"] = "alias-b"
            saved = _save_payload(tmp, document)
            for section in ("llm", "llm_remote"):
                self.assertNotIn("concurrency", saved[section], section)

    def test_unrelated_save_keeps_both_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, entry = self._seeded(tmp)
            document = _get_document(tmp)
            document["tts"]["pause_same_speaker_ms"] = 909
            saved = _save_payload(tmp, document)
            self.assertEqual(909, saved["tts"]["pause_same_speaker_ms"])
            for section in ("llm", "llm_local"):
                self.assertEqual(entry["concurrency"], saved[section]["concurrency"], section)

    def test_cold_benchmark_rejects_publication_after_changed_identity_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self._seeded(tmp, with_cache=False)

            def cold(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
                if on_measurement is not None:
                    on_measurement(1, 12.0, 0.01)
                self._save_change(tmp, "llm_local", base_url="http://127.0.0.1:6666/v1")
                return 4

            patches = _patch_benchmark()
            with patches[0], patches[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=cold):
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", ACTIVE["base_url"], ACTIVE["model_name"], object(),
                    status=_benchmark_status())
            self.assertEqual(4, value)
            saved = _load_config(path)
            self.assertEqual("http://127.0.0.1:6666/v1", saved["llm_local"]["base_url"])
            self.assertNotIn("concurrency", saved["llm_local"])

    def test_cold_benchmark_and_unrelated_save_both_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self._seeded(tmp, with_cache=False)

            def cold(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
                if on_measurement is not None:
                    on_measurement(1, 12.0, 0.01)
                document = _get_document(tmp)
                document["tts"]["pause_same_speaker_ms"] = 707
                _save_payload(tmp, document)
                return 4

            patches = _patch_benchmark()
            with patches[0], patches[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=cold):
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", ACTIVE["base_url"], ACTIVE["model_name"], object(),
                    status=_benchmark_status())
            saved = _load_config(path)
            self.assertEqual(4, value)
            self.assertEqual(707, saved["tts"]["pause_same_speaker_ms"])
            self.assertEqual(4, saved["llm_local"]["concurrency"])


class ProfileRoundTripTests(unittest.TestCase):
    def test_empty_and_key_only_legacy_round_trip_get_and_save(self):
        for legacy in ({}, {"api_key": "fixture"}):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                _seed_config(tmp, {"llm_mode": "local", "llm": legacy,
                                   "tts": {"mode": "local"}, "generation": {}})
                saved = _save_payload(tmp, _get_document(tmp))
                self.assertTrue(saved["llm"]["base_url"])
                self.assertTrue(saved["llm"]["model_name"])
                self.assertTrue(get_active_llm_config(saved)["base_url"])

    def test_endpoint_bound_key_only_legacy_keeps_its_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_config(tmp, {"llm_mode": "local",
                               "llm": {"base_url": "http://localhost:1234/v1", "api_key": "fixture"},
                               "tts": {"mode": "local"}, "generation": {}})
            document = _get_document(tmp)
            self.assertEqual("[REDACTED]", document["llm"]["api_key"])
            saved = _save_payload(tmp, document)
            self.assertEqual("fixture", saved["llm"]["api_key"])
            self.assertEqual("fixture", get_saved_llm_profile(saved)["api_key"])

    def test_url_less_manual_round_trips_without_endpoint_validation(self):
        manual = {"transport": "manual", "api_key": "fixture", "base_url": "", "model_name": ""}
        with tempfile.TemporaryDirectory() as tmp:
            _seed_config(tmp, {"llm_mode": "local", "llm": manual, "llm_local": manual,
                               "tts": {"mode": "local"}, "generation": {}})
            saved = _save_payload(tmp, _get_document(tmp))
            for section in ("llm", "llm_local"):
                self.assertEqual("manual", saved[section]["transport"], section)
                self.assertEqual("", saved[section]["base_url"], section)
                self.assertEqual("fixture", saved[section]["api_key"], section)
            self.assertEqual("manual", get_saved_llm_profile(saved)["transport"])


class ManualFailoverDispatchTests(unittest.TestCase):
    def test_manual_secondary_dispatches_serially_without_status_probe(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        class ManualCompletions:
            def __init__(self):
                self.lock = threading.Lock()
                self.live = self.peak = 0
                self.calls = []

            def create(self, *args, **kwargs):
                with self.lock:
                    self.live += 1
                    self.peak = max(self.peak, self.live)
                    self.calls.append(kwargs)
                try:
                    time.sleep(0.01)
                    return "ok"
                finally:
                    with self.lock:
                        self.live -= 1

        class FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {})()
                self.chat.completions = ManualCompletions()

            def with_options(self, **kwargs):
                return self

            def close(self):
                pass

        primary, secondary = FakeClient(), FakeClient()
        client = llm_provider.FailoverClient(
            primary, "primary-model", secondary, "manual-model", secondary_label="manual",
            secondary_config=dict(MANUAL, context_length=8192))
        client.failover({"category": "rate_limited"})
        variants = [client.with_options(timeout=3) for _ in range(8)]
        with patch("lmstudio_settings.get_current_status",
                   side_effect=AssertionError("manual transport must not probe")) as probe:
            with ThreadPoolExecutor(max_workers=8) as pool:
                replies = list(pool.map(lambda variant: variant.chat.completions.create(
                    model="primary-model", messages=[{"role": "user", "content": "hi"}],
                    max_tokens=16), variants))
            profile = client.ensure_active_runtime_profile()
        probe.assert_not_called()
        self.assertEqual(["ok"] * 8, replies)
        self.assertEqual(1, secondary.chat.completions.peak)
        self.assertEqual(["manual-model"] * 8,
                         [call["model"] for call in secondary.chat.completions.calls])
        self.assertEqual(8192, profile["context_length"])
        self.assertTrue(profile["status"]["manual"])


class PreviewRetentionTests(unittest.TestCase):
    def test_preview_counts_only_fields_save_actually_drops(self):
        entry = _cache_entry(_benchmark_environment(_benchmark_status()))
        with tempfile.TemporaryDirectory() as tmp:
            _seed_config(tmp, {"llm_mode": "local", "llm": dict(ACTIVE, **entry),
                               "llm_local": dict(ACTIVE, **entry), "tts": {"mode": "local"},
                               "generation": {}})
            unchanged = _get_document(tmp)
            with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
                preview = asyncio.run(system_module.preview_config(unchanged))
            self.assertEqual([], [r for r in preview["changes"] if "concurrency" in r["path"]])
            saved = _save_payload(tmp, unchanged)
            for key in entry:
                self.assertEqual(entry[key], saved["llm_local"][key])

            changed = _get_document(tmp)
            changed["llm_local"]["reasoning_effort"] = "high"
            with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
                preview_changed = asyncio.run(system_module.preview_config(changed))
            saved_changed = _save_payload(tmp, changed)
            self.assertEqual([], [key for key in entry if key in saved_changed["llm_local"]])
            self.assertGreater(preview_changed["unmanaged_fields"], preview["unmanaged_fields"])


if __name__ == "__main__":
    unittest.main()
