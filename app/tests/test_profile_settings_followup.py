"""Follow-up regressions for bound benchmark identity, Setup hydration and classification.

All credentials are synthetic, clients are stubbed and every config lives in a
disposable temp directory. No network or GPU work is performed.
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import llm_bench
import llm_provider
import lmstudio_settings as settings
import routers.system as system_module
from config_settings import AppConfig, LLMConfig
from lmstudio_settings import (get_active_llm_config, get_failover_llm_config,
                               is_executable_failover_profile)

LOCAL = "http://127.0.0.1:5555/v1"
HOSTED = "https://api.openai.com/v1"
LOW = {"base_url": LOCAL, "api_key": "fixture", "model_name": "active-model",
       "reasoning_effort": "low"}
HIGH = {**LOW, "reasoning_effort": "high"}
MODEL = "active-model"
STATUS = {"available": True, "loaded": True, "parallel": 8, "context_length": 8192,
          "runtime": "lmstudio"}
HOST, GPU, BACKEND = "fixture-host", "fixture-gpu", "fixture-backend"


def _find(value):
    def find(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
        if on_measurement is not None:
            on_measurement(1, 10.0, 0.01)
        return value
    return find


def _seed(tmp, document):
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _get(tmp):
    with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
        return asyncio.run(system_module.get_config())


def _preview(tmp, document):
    with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
        return asyncio.run(system_module.preview_config(document))


def _save(tmp, document):
    path = os.path.join(tmp, "config.json")
    with patch.object(system_module, "CONFIG_PATH", path), \
         patch.object(system_module.project_manager, "invalidate_config_cache"), \
         patch.object(system_module.project_manager, "engine", None):
        asyncio.run(system_module.save_config(AppConfig.model_validate(document)))
    return _load(path)


def _form(document):
    """The production JS posts these whole profile objects back unchanged."""
    form = {key: document[key] for key in
            ("llm_mode", "llm", "llm_local", "llm_remote", "llm_failover", "llm_remote_ssh",
             "tts", "generation")
            if key in document}
    for section in ("llm_local", "llm_remote"):
        if form.get(section) is None:
            form[section] = {"base_url": "", "api_key": "local", "model_name": ""}
    return form


def _environment(base_url=LOCAL, model=MODEL, remote=False):
    return llm_bench.get_concurrency_environment(base_url, model, remote, None,
                                                  HOST, GPU, BACKEND, STATUS)


def _entry(environment=None, profile=LOW):
    environment = _environment() if environment is None else environment
    return {"concurrency": 5, "concurrency_for": f"{profile['base_url']}::{profile['model_name']}",
            "concurrency_environment": environment, "concurrency_revision": "fixture-revision",
            settings.BENCHMARK_IDENTITY_KEY: settings.get_benchmark_identity_key(
                {"llm_mode": "local", "llm": profile, "llm_local": profile}, "llm_local")}


def _patches(remote=False):
    if remote:
        return patch.object(llm_bench.lmstudio_settings, "get_remote_gpu_name_and_backend",
                            return_value=(GPU, BACKEND))
    return patch.object(llm_bench.platform, "node", return_value=HOST), \
        patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                     return_value=(GPU, BACKEND))


class BoundBenchmarkIdentityTests(unittest.TestCase):
    def _run(self, path, profile, status=STATUS, **overrides):
        patches = _patches(profile["base_url"] == HOSTED)
        with patches[0], patches[1], patch.object(
                llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
            value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                path, "local", profile["base_url"], profile["model_name"], object(),
                status=status, llm_config=profile, **overrides)
        return value, bench

    def test_edit_before_entry_is_published_as_bound_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                               "tts": {"mode": "local"}, "generation": {}})
            _save(tmp, _form({**_get(tmp), "llm": dict(HIGH), "llm_local": dict(HIGH)}))
            value, bench = self._run(path, LOW)
            self.assertEqual(3, value)
            bench.assert_called_once()
            saved = _load(path)
            self.assertEqual(settings.get_benchmark_identity_key(
                {"llm_mode": "local", "llm": LOW, "llm_local": LOW}, "llm_local"),
                saved["llm_local"][settings.BENCHMARK_IDENTITY_KEY])

            value, bench = self._run(path, HIGH)
            self.assertEqual(3, value)
            bench.assert_called_once()  # high-effort run must not reuse the low one

    def test_body_only_effective_change_drops_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, **_entry()),
                               "llm_local": dict(LOW, **_entry()), "tts": {"mode": "local"},
                               "generation": {}})
            value, bench = self._run(path, LOW)
            self.assertEqual(5, value)
            bench.assert_not_called()

            document = _get(tmp)
            document["llm_local"]["provider_extra_body"] = {"enable_thinking": False}
            saved = _save(tmp, _form(document))
            for key in ("concurrency", settings.BENCHMARK_IDENTITY_KEY):
                self.assertNotIn(key, saved["llm_local"], key)
            value, bench = self._run(path, {**LOW, "provider_extra_body": {"enable_thinking": False}})
            self.assertEqual(3, value)
            bench.assert_called_once()

    def test_equivalent_body_spelling_keeps_the_measurement(self):
        typed = dict(LOW)
        body_only = {key: value for key, value in LOW.items() if key != "reasoning_effort"}
        body_only["provider_extra_body"] = {"reasoning_effort": "low"}
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(typed, **_entry()),
                               "llm_local": dict(typed, **_entry()), "tts": {"mode": "local"},
                               "generation": {}})
            value, bench = self._run(path, body_only)
            self.assertEqual(5, value)
            bench.assert_not_called()

    def test_edit_during_sweep_still_refuses_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                               "tts": {"mode": "local"}, "generation": {}})

            def cold(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
                on_measurement(1, 12.0, 0.01)
                document = _get(tmp)
                document["llm_local"]["base_url"] = "http://127.0.0.1:6666/v1"
                document["llm_local"]["api_key"] = "fixture-rotated"
                _save(tmp, _form(document))
                return 4

            patches = _patches()
            with patches[0], patches[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=cold):
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", LOW["base_url"], LOW["model_name"], object(),
                    status=STATUS, llm_config=LOW)
            self.assertEqual(4, value)
            self.assertNotIn("concurrency", _load(path)["llm_local"])

    def test_unrelated_save_keeps_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, **_entry()),
                               "llm_local": dict(LOW, **_entry()), "tts": {"mode": "local"},
                               "generation": {}})
            document = _get(tmp)
            document["tts"]["pause_same_speaker_ms"] = 321
            saved = _save(tmp, _form(document))
            self.assertEqual(321, saved["tts"]["pause_same_speaker_ms"])
            value, bench = self._run(path, LOW)
            self.assertEqual(5, value)
            bench.assert_not_called()


class SetupSlotHydrationTests(unittest.TestCase):
    def test_complete_legacy_hydrates_neutral_slots_in_both_modes(self):
        neutral = LLMConfig(base_url="", api_key="local", model_name="").model_dump()
        for mode in ("local", "remote"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                _seed(tmp, {"llm_mode": mode, "llm": dict(LOW), "llm_local": dict(neutral),
                            "llm_remote": dict(neutral), "tts": {"mode": "local"},
                            "generation": {}})
                document = _get(tmp)
                active = f"llm_{mode}"
                for section in ("llm", active):
                    self.assertEqual(LOCAL, document[section]["base_url"], section)
                    self.assertEqual(MODEL, document[section]["model_name"], section)
                inactive = "llm_remote" if mode == "local" else "llm_local"
                self.assertTrue(all(key in document[inactive]
                                    for key in ("base_url", "api_key", "model_name")))
                _preview(tmp, _form(document))
                saved = _save(tmp, _form(document))
                for section in ("llm", active):
                    self.assertEqual(LOCAL, saved[section]["base_url"], section)
                    self.assertEqual("fixture", saved[section]["api_key"], section)
                self.assertTrue(all(key in saved[inactive]
                                    for key in ("base_url", "api_key", "model_name")))

    def test_key_only_inactive_slot_and_explicit_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = {"base_url": LOCAL, "api_key": "inactive-fixture", "model_name": "remote-model"}
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                        "llm_remote": dict(remote), "tts": {"mode": "local"}, "generation": {}})
            document = _get(tmp)
            self.assertEqual("[REDACTED]", document["llm"]["api_key"])
            self.assertEqual("remote-model", document["llm_remote"]["model_name"])
            saved = _save(tmp, _form(document))
            self.assertEqual("fixture", saved["llm"]["api_key"])
            self.assertEqual(LOCAL, saved["llm_remote"]["base_url"])
            self.assertEqual("inactive-fixture", saved["llm_remote"]["api_key"])

        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                        "llm_remote": {"api_key": "inactive-fixture"}, "tts": {"mode": "local"},
                        "generation": {}})
            document = _get(tmp)
            self.assertTrue(document["llm_remote"]["base_url"])
            self.assertEqual("local", document["llm_remote"]["api_key"])
            saved = _save(tmp, _form(document))
            self.assertTrue(saved["llm_remote"]["base_url"])


class HostedLegacyMigrationTests(unittest.TestCase):
    def _seed_hosted(self, tmp, *, invalid_named=False):
        hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model"}
        entry = _entry(_environment(HOSTED, "hosted-model", remote=True), hosted)
        document = {"llm_mode": "local", "llm": dict(hosted, **entry),
                    "llm_local": dict(hosted), "tts": {"mode": "local"}, "generation": {}}
        if invalid_named:
            document["llm_remote"] = dict(hosted, **_entry({}, hosted))
        return _seed(tmp, document), hosted, entry

    def _run_hosted(self, path, hosted):
        patches = _patches(remote=True)
        with patches, patch.object(llm_bench, "find_optimal_concurrency",
                                   side_effect=AssertionError("migrated cache must be reused")):
            return llm_bench.ensure_cached_or_benchmarked_concurrency(
                path, "local", HOSTED, "hosted-model", object(), status=STATUS,
                llm_config=hosted)

    def test_hosted_legacy_cache_migrates_to_benchmark_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, hosted, entry = self._seed_hosted(tmp)
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual(entry["concurrency"], saved["llm_remote"]["concurrency"])
            self.assertEqual(entry["concurrency_environment"],
                             saved["llm_remote"]["concurrency_environment"])
            self.assertEqual(entry["concurrency"], self._run_hosted(path, hosted))

    def test_invalid_named_environment_does_not_displace_legacy_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, hosted, entry = self._seed_hosted(tmp, invalid_named=True)
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual(entry["concurrency_environment"],
                             saved["llm_remote"]["concurrency_environment"])
            self.assertEqual(entry["concurrency"], self._run_hosted(path, hosted))


class FailoverEndpointValidationTests(unittest.TestCase):
    def test_predicate_requires_usable_endpoint_and_trimmed_model(self):
        self.assertTrue(is_executable_failover_profile(dict(LOW)))
        self.assertTrue(is_executable_failover_profile({"transport": "manual", "api_key": "fixture"}))
        for bad in ({"base_url": "http://[", "model_name": MODEL},
                    {"base_url": LOCAL, "model_name": "   "},
                    {"base_url": LOCAL, "model_name": ""}):
            self.assertFalse(is_executable_failover_profile(bad), bad)

    def test_save_get_client_rejects_blank_model_and_keeps_valid_secondary(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = {"base_url": LOCAL, "api_key": "fixture", "model_name": "   "}
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                        "llm_remote": remote, "llm_failover": True, "tts": {"mode": "local"},
                        "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual({}, get_failover_llm_config(saved))
            with patch.object(llm_provider, "make_llm_client") as make:
                make.return_value = primary = object()
                self.assertIs(primary, llm_provider.make_run_client(
                    saved, get_active_llm_config(saved), 30))
                self.assertEqual(1, make.call_count)

        with tempfile.TemporaryDirectory() as tmp:
            remote = {"base_url": "http://127.0.0.1:6666/v1", "api_key": "fixture",
                      "model_name": "secondary-model"}
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                        "llm_remote": remote, "llm_failover": True, "tts": {"mode": "local"},
                        "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual("secondary-model", get_failover_llm_config(saved)["model_name"])
            with patch.object(llm_provider, "make_llm_client") as make:
                make.side_effect = [object(), object()]
                client = llm_provider.make_run_client(saved, get_active_llm_config(saved), 30)
                self.assertIsInstance(client, llm_provider.FailoverClient)
                self.assertEqual(2, make.call_count)


class ManualClassificationTests(unittest.TestCase):
    def test_manual_is_not_probed_or_billed_even_with_malformed_url(self):
        manual = {"transport": "manual", "api_key": "fixture", "base_url": "http://[",
                  "model_name": "manual-model"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            _seed(tmp, {"llm_mode": "local", "llm": dict(manual), "llm_local": dict(manual),
                        "llm_remote": dict(manual), "llm_failover": True,
                        "tts": {"mode": "local"}, "generation": {}})
            with patch.object(system_module, "CONFIG_PATH", path), \
                 patch.object(system_module, "get_current_status",
                              side_effect=AssertionError("manual must not be probed")):
                status = asyncio.run(system_module.lmstudio_status())
                document = asyncio.run(system_module.get_config())
            self.assertTrue(status["manual"])
            self.assertFalse(status["available"])
            self.assertFalse(document["is_remote"])
            self.assertFalse(document["failover_is_remote"])
            self.assertEqual("manual", get_failover_llm_config(document)["transport"])


class PreviewDroppedFieldTests(unittest.TestCase):
    def test_migrated_cache_is_not_counted_but_unknown_fields_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = _entry()
            legacy = dict(LOW, **entry, legacy_only="fixture-note")
            _seed(tmp, {"llm_mode": "local", "llm": legacy, "llm_local": dict(LOW, **entry),
                        "tts": {"mode": "local"}, "generation": {}})
            preview = _preview(tmp, _form(_get(tmp)))
            self.assertEqual(1, preview["unmanaged_fields"], preview["unmanaged_fields"])
            self.assertTrue(any(row["path"].startswith("llm_local.")
                                for row in preview["changes"] + preview["defaults"]))

    def test_truly_dropped_named_unknown_field_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW),
                        "llm_local": dict(LOW, mystery_field="fixture"), "tts": {"mode": "local"},
                        "generation": {}})
            preview = _preview(tmp, _form(_get(tmp)))
            self.assertGreaterEqual(preview["unmanaged_fields"], 1)
            saved = _save(tmp, _form(_get(tmp)))
            self.assertNotIn("mystery_field", saved["llm_local"])


if __name__ == "__main__":
    unittest.main()
