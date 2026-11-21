"""Follow-up regressions for bound benchmark identity, Setup hydration and classification.

All credentials are synthetic, clients are stubbed and every config lives in a
disposable temp directory. No network or GPU work is performed.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config_change_preview
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


def _save_preview(tmp, document):
    from fastapi import HTTPException
    with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
        try:
            asyncio.run(system_module.preview_config(document))
            return 200
        except HTTPException as error:
            return error.status_code


def _post_unsafe(client, url, payload):
    """Send a payload Python can hold but strict JSON encoders refuse (Inf)."""
    return client.post(url, content=json.dumps(payload, allow_nan=True),
                       headers={"content-type": "application/json"})


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


class BoundProvenanceRegressionTests(unittest.TestCase):
    def test_ceiling_is_captured_at_binding_not_reinherited_at_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                        "tts": {"mode": "local"}, "generation": {"max_input_tokens": 4096}}
            path = _seed(tmp, document)
            uncapped = settings.get_benchmark_identity_key(document, "llm_local", profile=dict(LOW))
            capped = settings.get_benchmark_identity_key(
                document, "llm_local", profile=dict(LOW, max_input_tokens=4096))
            identity = settings.get_benchmark_identity(document, "llm_local", profile=dict(LOW))
            self.assertIsNone(identity["max_input_tokens"])
            self.assertNotEqual(uncapped, capped)

            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", LOW["base_url"], LOW["model_name"], object(),
                    status=STATUS, llm_config=dict(LOW))
            bench.assert_called_once()
            self.assertEqual(uncapped, _load(path)["llm_local"][settings.BENCHMARK_IDENTITY_KEY])

            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(4)) as bench:
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", LOW["base_url"], LOW["model_name"], object(),
                    status=STATUS, llm_config=dict(LOW, max_input_tokens=4096))
            self.assertEqual(4, value)
            bench.assert_called_once()  # the new client owns a distinct sweep
            self.assertEqual(capped, _load(path)["llm_local"][settings.BENCHMARK_IDENTITY_KEY])

    def test_bound_management_alias_overrides_the_disk_alias(self):
        profile = dict(LOW, base_url=HOSTED, model_name="hosted-model")
        config = {"llm_mode": "local", "llm_remote_ssh": "disk-fixture-alias"}
        bound = settings.get_benchmark_identity(config, "llm_remote", profile=profile,
                                                ssh_alias="bound-fixture-alias")
        self.assertEqual("bound-fixture-alias", bound["ssh_alias"])
        derived = settings.get_benchmark_identity(config, "llm_remote", profile=profile)
        self.assertEqual("disk-fixture-alias", derived["ssh_alias"])


class HostedReceiptSlotTests(unittest.TestCase):
    @staticmethod
    def _run_hosted(path, hosted):
        with _patches(remote=True), patch.object(
                llm_bench, "find_optimal_concurrency",
                side_effect=AssertionError("migrated cache must be reused")):
            return llm_bench.ensure_cached_or_benchmarked_concurrency(
                path, "local", HOSTED, "hosted-model", object(), status=STATUS,
                llm_config=hosted)

    def test_fresh_named_hosted_receipt_survives_an_unrelated_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model"}
            environment = _environment(HOSTED, "hosted-model", remote=True)
            entry = _entry(environment, hosted)
            # The endpoint-based slot was hydrated to a neutral stub after the
            # measurement; its recorded destination still qualifies the receipt.
            stub = {"base_url": "", "api_key": "local", "model_name": "", **entry}
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(hosted),
                               "llm_local": dict(hosted), "llm_remote": stub,
                               "tts": {"mode": "local"}, "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual(entry["concurrency"], saved["llm_remote"]["concurrency"])
            self.assertEqual(environment, saved["llm_remote"]["concurrency_environment"])
            self.assertEqual(entry["concurrency"], self._run_hosted(path, hosted))

    def test_named_evidence_wins_over_the_legacy_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model"}
            environment = _environment(HOSTED, "hosted-model", remote=True)
            mirror = dict(hosted, **_entry(environment, hosted))
            mirror["concurrency"] = 2
            named = dict(hosted, **_entry(environment, hosted))
            named["concurrency"] = 7
            path = _seed(tmp, {"llm_mode": "local", "llm": mirror, "llm_local": dict(hosted),
                               "llm_remote": named, "tts": {"mode": "local"}, "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual(7, saved["llm_remote"]["concurrency"])
            self.assertEqual(7, self._run_hosted(path, hosted))


class SetupProvenanceRegressionTests(unittest.TestCase):
    def test_two_named_credentials_on_equal_endpoints_follow_the_active_role(self):
        endpoint = "http://127.0.0.1:7777/v1"
        local = {**LOW, "base_url": endpoint, "api_key": "local-fixture",
                 "provider_headers": {"x-local-fixture": "local-header"}}
        remote = {**LOW, "base_url": endpoint, "api_key": "remote-fixture",
                  "provider_headers": {"x-remote-fixture": "remote-header"}}
        for mode, expected_key, expected_header in (
                ("local", "local-fixture", "local-header"),
                ("remote", "remote-fixture", "remote-header")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                active = local if mode == "local" else remote
                _seed(tmp, {"llm_mode": mode, "llm": dict(active), "llm_local": dict(local),
                            "llm_remote": dict(remote), "tts": {"mode": "local"},
                            "generation": {}})
                document = _get(tmp)
                self.assertEqual(system_module._REDACTED_SECRET,
                                 document["llm_local"]["api_key"])
                saved = _save(tmp, _form(document))
                self.assertEqual(expected_key, saved["llm"]["api_key"])
                header_name = "x-local-fixture" if mode == "local" else "x-remote-fixture"
                self.assertEqual(expected_header, saved["llm"]["provider_headers"][header_name])
                self.assertEqual("local-fixture", saved["llm_local"]["api_key"])
                self.assertEqual("remote-fixture", saved["llm_remote"]["api_key"])

    def test_headers_restore_independently_of_the_api_key_field(self):
        profile = dict(LOW, provider_headers={"x-acme-fixture": "header-fixture"})
        for key in ("env:FIXTURE_KEY_REFERENCE", "", "typed-fixture-key"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                            "llm_local": dict(profile), "tts": {"mode": "local"},
                            "generation": {}})
                document = _get(tmp)
                document["llm_local"]["api_key"] = key
                saved = _save(tmp, _form(document))
                self.assertEqual(key, saved["llm_local"]["api_key"])
                self.assertEqual("header-fixture",
                                 saved["llm_local"]["provider_headers"]["x-acme-fixture"])


class EndpointAndDigestRegressionTests(unittest.TestCase):
    def test_malformed_port_refused_and_padded_endpoint_trimmed(self):
        self.assertTrue(settings.is_usable_llm_endpoint(f"  {LOCAL}  "))
        self.assertEqual(LOCAL, settings.normalize_llm_endpoint(f"  {LOCAL}  "))
        for bad in ("http://127.0.0.1:abc/v1", "http://127.0.0.1:99999/v1", "http://["):
            self.assertFalse(settings.is_usable_llm_endpoint(bad), bad)
            self.assertFalse(is_executable_failover_profile(
                {"base_url": bad, "model_name": MODEL}), bad)

    def test_runtime_client_uses_trimmed_endpoint_and_model(self):
        with patch.object(llm_provider, "OpenAI") as make:
            llm_provider.make_llm_client(dict(LOW, base_url=f"  {LOCAL}  "), 30)
        self.assertEqual(LOCAL, make.call_args.kwargs["base_url"])

        secondary = {"base_url": "http://127.0.0.1:6666/v1", "api_key": "fixture",
                     "model_name": "secondary-model"}
        with patch.object(llm_provider, "make_llm_client", side_effect=[object(), object()]):
            client = llm_provider.make_run_client(
                {"llm_mode": "local", "llm_failover": True, "llm_remote": secondary},
                dict(LOW, model_name="  active-model  "), 30)
        self.assertEqual("active-model", client._pair[0][1])
        self.assertEqual("secondary-model", client._pair[1][1])

    def test_identity_is_a_versioned_nonsecret_digest(self):
        profile = dict(LOW, provider_extra_body={"retired_fixture_marker": "fixture-secret-value"})
        key = settings.get_benchmark_identity_key(
            {"llm_mode": "local", "llm": profile}, "llm", profile=profile)
        self.assertTrue(key.startswith("v1:"), key)
        self.assertNotIn("fixture-secret-value", key)
        self.assertNotIn("retired_fixture_marker", key)

    def test_setup_output_omits_benchmark_metadata_but_disk_keeps_it(self):
        entry = _entry()
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, **entry),
                        "llm_local": dict(LOW, **entry), "tts": {"mode": "local"},
                        "generation": {}})
            document = _get(tmp)
            for section in ("llm", "llm_local"):
                for key in settings.BENCHMARK_CACHE_KEYS:
                    self.assertNotIn(key, document[section], (section, key))
            saved = _save(tmp, _form(document))
            self.assertEqual(entry["concurrency"], saved["llm_local"]["concurrency"])

    def test_retired_body_value_is_absent_after_save(self):
        marker = "fixture-retired-body-value"
        old = dict(LOW, provider_extra_body={"marker": marker})
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(old, **_entry(profile=old)),
                        "llm_local": dict(old, **_entry(profile=old)),
                        "tts": {"mode": "local"}, "generation": {}})
            document = _get(tmp)
            document["llm_local"]["provider_extra_body"] = {"marker": "fixture-new-body-value"}
            saved = _save(tmp, _form(document))
            self.assertNotIn(marker, json.dumps(saved))


class PreviewCountRegressionTests(unittest.TestCase):
    def test_legacy_only_section_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, legacy_only="fixture-note"),
                        "tts": {"mode": "local"}, "generation": {}})
            preview = _preview(tmp, _form(_get(tmp)))
            self.assertEqual(1, preview["unmanaged_fields"], preview["unmanaged_fields"])

    def test_unknown_field_sharing_a_value_is_not_treated_as_cache_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = {"base_url": "http://127.0.0.1:6666/v1", "api_key": "fixture",
                      "model_name": "other-model", "stray": "fixture-shared-value"}
            _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, stray="fixture-shared-value"),
                        "llm_remote": remote, "tts": {"mode": "local"}, "generation": {}})
            preview = _preview(tmp, _form(_get(tmp)))
            self.assertEqual(2, preview["unmanaged_fields"], preview["unmanaged_fields"])


class SetupHydrationRoundTripTests(unittest.TestCase):
    def test_neutral_active_slot_round_trips_shared_endpoint_in_both_modes(self):
        endpoint = "http://127.0.0.1:7878/v1"
        for mode in ("local", "remote"):
            for key_kind, key in (("stored", "legacy-fixture-key"),
                                  ("env", "env:FIXTURE_ENV_KEY"),
                                  ("blank", "")):
                with self.subTest(mode=mode, key_kind=key_kind), tempfile.TemporaryDirectory() as tmp:
                    legacy = {"base_url": endpoint, "api_key": key, "model_name": "legacy-model",
                              "provider_headers": {"x-legacy-fixture": "legacy-header"}}
                    inactive = {"base_url": endpoint, "api_key": "inactive-fixture-key",
                                "model_name": "inactive-model",
                                "provider_headers": {"x-inactive-fixture": "inactive-header"}}
                    neutral = LLMConfig(base_url="", api_key="", model_name="").model_dump()
                    active_section = f"llm_{mode}"
                    inactive_section = "llm_remote" if mode == "local" else "llm_local"
                    _seed(tmp, {"llm_mode": mode, "llm": dict(legacy),
                                active_section: dict(neutral), inactive_section: dict(inactive),
                                "tts": {"mode": "local"}, "generation": {}})

                    hydrated = _get(tmp)
                    self.assertEqual(endpoint, hydrated[active_section]["base_url"])
                    self.assertIn("changes", _preview(tmp, _form(hydrated)))
                    saved = _save(tmp, _form(hydrated))

                    self.assertEqual(key, saved[active_section]["api_key"])
                    self.assertEqual("legacy-header",
                                     saved[active_section]["provider_headers"]["x-legacy-fixture"])
                    self.assertEqual("inactive-fixture-key", saved[inactive_section]["api_key"])
                    self.assertEqual("inactive-header",
                                     saved[inactive_section]["provider_headers"]["x-inactive-fixture"])


class LegacyReceiptSanitizationTests(unittest.TestCase):
    def _predigest_receipt(self, profile, marker):
        entry = _entry(_environment(profile["base_url"], profile["model_name"], remote=True),
                       profile)
        entry[settings.BENCHMARK_IDENTITY_KEY] = json.dumps(
            {"base_url": profile["base_url"],
             "provider_extra_body": {"marker": marker}})
        return entry

    def test_hydrated_output_carries_no_receipt_fields_or_raw_identity(self):
        marker = "fixture-retired-body-value"
        hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model",
                  "provider_extra_body": {"marker": marker}}
        neutral = LLMConfig(base_url="", api_key="", model_name="").model_dump()
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(hosted, **self._predigest_receipt(hosted, marker)),
                        "llm_local": dict(neutral), "tts": {"mode": "local"}, "generation": {}})
            hydrated = _get(tmp)
            for section in ("llm", "llm_local"):
                for key in settings.BENCHMARK_CACHE_KEYS:
                    self.assertNotIn(key, hydrated[section], (section, key))

    def test_predigest_identity_is_replaced_and_retired_body_value_dropped(self):
        marker = "fixture-retired-body-value"
        hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model",
                  "provider_extra_body": {"marker": marker}}
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(hosted, **self._predigest_receipt(hosted, marker)),
                        "llm_local": dict(hosted), "tts": {"mode": "local"}, "generation": {}})
            document = _form(_get(tmp))
            document["llm_local"]["provider_extra_body"] = {"marker": "fixture-new-body-value"}
            saved = _save(tmp, document)
            self.assertNotIn(marker, json.dumps(saved))
            for section in ("llm", "llm_local"):
                stored = saved[section].get(settings.BENCHMARK_IDENTITY_KEY)
                if stored is not None:
                    self.assertTrue(settings.is_benchmark_digest(stored), stored)


class ReceiptQualificationTests(unittest.TestCase):
    def test_only_receipt_matching_the_proposed_request_migrates(self):
        hosted = {"base_url": HOSTED, "api_key": "fixture", "model_name": "hosted-model"}
        stale = _entry(_environment(HOSTED, "old-model", remote=True),
                       dict(hosted, model_name="old-model"))
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(hosted, **stale),
                        "llm_local": dict(hosted), "tts": {"mode": "local"}, "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertNotIn(settings.BENCHMARK_IDENTITY_KEY, saved.get("llm_remote", {}))
            self.assertNotIn("concurrency", saved.get("llm_remote", {}))


class BoundModeDigestTests(unittest.TestCase):
    def test_bound_mode_drives_digest_and_slot_after_a_setup_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "remote", "llm": dict(LOW), "llm_local": dict(LOW),
                               "llm_remote": dict(LOW), "llm_remote_ssh": None,
                               "tts": {"mode": "local"}, "generation": {}})
            config = _load(path)
            bound = settings.get_benchmark_identity_key(
                config, "llm_local", profile=dict(LOW), llm_mode="local", ssh_alias=None)
            switched = settings.get_benchmark_identity_key(
                config, "llm_local", profile=dict(LOW), llm_mode="remote", ssh_alias=None)
            self.assertNotEqual(bound, switched)

            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(3)):
                llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", LOW["base_url"], LOW["model_name"], object(),
                    status=STATUS, llm_config=dict(LOW))
            saved = _load(path)
            self.assertEqual(3, saved["llm_local"]["concurrency"])
            self.assertEqual(bound, saved["llm_local"][settings.BENCHMARK_IDENTITY_KEY])
            self.assertNotIn("concurrency", saved.get("llm_remote", {}))

    def test_adjacent_large_integer_seeds_have_distinct_digests(self):
        first = dict(LOW, provider_extra_body={"seed": 9007199254740992})
        second = dict(LOW, provider_extra_body={"seed": 9007199254740993})
        first_key = settings.get_benchmark_identity_key(
            {"llm_mode": "local", "llm": first}, "llm", profile=first)
        second_key = settings.get_benchmark_identity_key(
            {"llm_mode": "local", "llm": second}, "llm", profile=second)
        self.assertNotEqual(first_key, second_key)

    def test_malformed_hosts_and_zero_port_rejected_ipv6_preserved(self):
        for bad in ("http://bad host:1234/v1", "http://host:0/v1", "http://\thost/v1"):
            self.assertFalse(settings.is_usable_llm_endpoint(bad), bad)
            self.assertFalse(is_executable_failover_profile(
                {"base_url": bad, "model_name": MODEL}), bad)
        for good in ("http://[::1]:1234/v1", "  http://127.0.0.1:1234/v1  "):
            self.assertTrue(settings.is_usable_llm_endpoint(good), good)
        self.assertEqual("http://[::1]:1234/v1",
                         settings.normalize_llm_endpoint("  http://[::1]:1234/v1 "))


class PreviewReceiptAccountingTests(unittest.TestCase):
    def test_equal_field_from_another_receipt_does_not_hide_a_drop(self):
        first = {"base_url": "http://127.0.0.1:7575/v1", "api_key": "fixture", "model_name": "model-a"}
        second = {"base_url": "http://127.0.0.1:7676/v1", "api_key": "fixture", "model_name": "model-b"}
        first_receipt = _entry(_environment(first["base_url"], "model-a"), first)
        second_receipt = _entry(_environment(second["base_url"], "model-b"), second)
        second_receipt["concurrency"] = first_receipt["concurrency"]
        with tempfile.TemporaryDirectory() as tmp:
            _seed(tmp, {"llm_mode": "local", "llm": dict(first),
                        "llm_local": dict(first, **first_receipt),
                        "llm_remote": dict(second, **second_receipt),
                        "tts": {"mode": "local"}, "generation": {}})
            document = _form(_get(tmp))
            document["llm_local"]["reasoning_effort"] = "high"
            preview = _preview(tmp, document)
            self.assertGreaterEqual(preview["unmanaged_fields"], len(settings.BENCHMARK_CACHE_KEYS))


class AdapterOnlyPrecedenceTests(unittest.TestCase):
    def test_named_adapter_only_profile_beats_complete_legacy_http_in_both_modes(self):
        from attribution_adapter import AdapterError, adapter_config, check_adapter
        for mode in ("local", "remote"):
            with self.subTest(mode=mode):
                cfg = {"llm_mode": mode,
                       "llm": {"base_url": "http://localhost:1234/v1", "api_key": "fixture",
                               "model_name": "legacy-model"},
                       f"llm_{mode}": {"attribution_adapter": {
                           "path": "/fixture/adapter.gguf", "require": True}}}
                self.assertEqual("/fixture/adapter.gguf", adapter_config(cfg)["path"])
                with patch("attribution_adapter.served_adapters", return_value=[]), \
                        self.assertRaises(AdapterError):
                    check_adapter(cfg, "http://x/v1", require=True)


FORM_JS = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('static/js/app-core.js', 'utf8');
const profiles = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const document = {getElementById(id) { return elements[id] ??= {value: ''}; }};
const context = {document, window: {}, llmProfiles: profiles,
                 getOptionalTokenInput: () => null, getOptionalNumberInput: () => null};
const code = source.slice(source.indexOf('        function populateLlmInputs('),
                          source.indexOf('        function syncCurrentLlmProfile('));
process.stdout.write(JSON.stringify(vm.runInNewContext(
    code + '\npopulateLlmInputs("local"); getEditedLlmProfile();', context)));
"""


def _form_via_js(document):
    result = subprocess.run(["node", "-e", FORM_JS], input=json.dumps(
        {"local": document["llm_local"]}), text=True, check=True, capture_output=True, timeout=20)
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ProductionSetupFormTests(unittest.TestCase):
    def test_blank_key_and_large_seed_survive_real_form_and_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = dict(LOW, api_key="", provider_headers={"x-fixture": "fixture-header"},
                          provider_extra_body={"seed": 9007199254740993})
            path = _seed(tmp, {"llm_mode": "local", "llm": legacy,
                               "llm_local": LLMConfig(base_url="", api_key="", model_name="").model_dump(),
                               "llm_remote": dict(LOW, model_name="inactive"),
                               "tts": {}, "generation": {}})
            document = _get(tmp)
            posted = _form_via_js(document)
            saved = _save(tmp, {**_form(document), "llm": posted, "llm_local": posted})
            self.assertEqual("", saved["llm_local"]["api_key"])
            self.assertEqual(9007199254740993, saved["llm_local"]["provider_extra_body"]["seed"])
            self.assertEqual({"x-fixture": "fixture-header"},
                             saved["llm_local"]["provider_headers"])


class ModeSwitchProvenanceTests(unittest.TestCase):
    def test_switching_mode_preserves_both_slots_at_a_shared_endpoint(self):
        endpoint = "http://127.0.0.1:7272/v1"
        for saved_mode, legacy_key in (("local", "legacy-fixture"),
                                       ("remote", "env:FIXTURE_KEY")):
            with self.subTest(saved_mode=saved_mode), tempfile.TemporaryDirectory() as tmp:
                legacy = {"base_url": endpoint, "api_key": legacy_key, "model_name": "legacy-model",
                          "provider_headers": {"x-legacy": "legacy-header"}}
                inactive = {"base_url": endpoint, "api_key": "inactive-fixture",
                            "model_name": "inactive-model",
                            "provider_headers": {"x-inactive": "inactive-header"}}
                old_active = "llm_local" if saved_mode == "local" else "llm_remote"
                new_active = "llm_remote" if saved_mode == "local" else "llm_local"
                _seed(tmp, {"llm_mode": saved_mode, "llm": dict(legacy),
                            old_active: LLMConfig(base_url="", api_key="", model_name="").model_dump(),
                            new_active: dict(inactive), "tts": {}, "generation": {}})
                form = _form(_get(tmp))
                form["llm_mode"] = new_active.removeprefix("llm_")
                saved = _save(tmp, form)
                self.assertEqual(legacy_key, saved[old_active]["api_key"])
                self.assertEqual({"x-legacy": "legacy-header"},
                                 saved[old_active]["provider_headers"])
                self.assertEqual("inactive-fixture", saved[new_active]["api_key"])
                self.assertEqual({"x-inactive": "inactive-header"},
                                 saved[new_active]["provider_headers"])


class StalePredigestReceiptTests(unittest.TestCase):
    @staticmethod
    def _receipt(profile):
        entry = _entry(profile=profile)
        entry[settings.BENCHMARK_IDENTITY_KEY] = json.dumps(
            settings.get_benchmark_identity({"llm_mode": "local", "llm": profile}, "llm"))
        return entry

    def test_changed_body_before_setup_forces_a_fresh_benchmark(self):
        old = dict(LOW, provider_extra_body={"marker": "fixture-old"})
        new = dict(LOW, provider_extra_body={"marker": "fixture-new"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(new, **self._receipt(old)),
                               "llm_local": LLMConfig(base_url="", api_key="", model_name="").model_dump(),
                               "tts": {}, "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertNotIn("concurrency", saved["llm_local"])
            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", new["base_url"], new["model_name"], object(),
                    status=STATUS, llm_config=new)
            bench.assert_called_once()
            self.assertEqual(3, value)

    def test_matching_predigest_receipt_is_relabelled_and_reused(self):
        legacy = dict(LOW, provider_extra_body={"marker": "fixture-legacy"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(legacy, **self._receipt(legacy)),
                               "llm_local": dict(legacy), "tts": {}, "generation": {}})
            saved = _save(tmp, _form(_get(tmp)))
            self.assertEqual(5, saved["llm_local"].get("concurrency"))
            self.assertTrue(settings.is_benchmark_digest(
                saved["llm_local"][settings.BENCHMARK_IDENTITY_KEY]))
            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency",
                    side_effect=AssertionError("proven receipt must be reused")):
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", legacy["base_url"], legacy["model_name"], object(),
                    status=STATUS, llm_config=legacy)
            self.assertEqual(5, value)


class StrictEndpointApiTests(unittest.TestCase):
    def test_preview_and_save_reject_port_zero_and_keep_ipv6(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local",
                               "llm": dict(LOW, base_url="http://127.0.0.1:0/v1"),
                               "llm_local": dict(LOW, base_url="http://127.0.0.1:0/v1"),
                               "tts": {}, "generation": {}})
            app = FastAPI()
            app.include_router(system_module.router)
            client = TestClient(app, raise_server_exceptions=True)
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                payload = _form(_get(tmp))
                self.assertEqual(400, client.post("/api/config/preview", json=payload).status_code)
                self.assertEqual(400, client.post("/api/config", json=payload).status_code)

            path = _seed(tmp, {"llm_mode": "local",
                               "llm": dict(LOW, base_url="http://[::1]:1234/v1"),
                               "llm_local": dict(LOW, base_url="http://[::1]:1234/v1"),
                               "tts": {}, "generation": {}})
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                payload = _form(_get(tmp))
                self.assertEqual(200, client.post("/api/config/preview", json=payload).status_code)
                self.assertEqual(200, client.post("/api/config", json=payload).status_code)


class ExactIntegerBoundaryTests(unittest.TestCase):
    def _roundtrip(self, tmp, value, typed=False):
        body = {"seed": value} if typed is False else {"outer": {"seed": value}}
        profile = dict(LOW, provider_extra_body=body)
        path = _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                           "llm_local": dict(profile), "tts": {}, "generation": {}})
        document = _get(tmp)
        posted = _form_via_js(document)
        if typed:
            # A user typing the value into the editor: raw text is exact, the
            # browser's parsed object is lossy (None once JS overflows).
            posted["provider_extra_body_json"] = json.dumps(body)
            posted["provider_extra_body"] = ({"outer": {"seed": 1}} if value < 10 ** 300
                                             else {"outer": {"seed": None}})
        form = {**_form(document), "llm": posted, "llm_local": posted}
        preview = _save_preview(tmp, form)
        saved = _save(tmp, form)
        self.assertEqual(200, preview)
        return saved["llm_local"]["provider_extra_body"]

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_untouched_large_integers_survive_real_form_and_routes(self):
        for value in (2 ** 53 + 1, 2 ** 63 - 1, 10 ** 310):
            with self.subTest(value=str(value)[:20]), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual({"seed": value}, self._roundtrip(tmp, value))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_typed_nested_large_integers_survive_without_overflow(self):
        for value in (2 ** 63 - 1, 10 ** 310):
            with self.subTest(value=str(value)[:20]), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual({"outer": {"seed": value}},
                                 self._roundtrip(tmp, value, typed=True))


class ExplicitBodyRepresentationTests(unittest.TestCase):
    def test_structured_input_wins_over_stale_auxiliary_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW),
                               "llm_local": dict(LOW), "tts": {}, "generation": {}})
            app = FastAPI()
            app.include_router(system_module.router)
            client = TestClient(app, raise_server_exceptions=True)
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                form = _form(_get(tmp))
                form["llm_local"]["provider_extra_body"] = {"flag": 1}
                form["llm"]["provider_extra_body"] = {"flag": 1}
                form["llm_local"]["provider_extra_body_json"] = '{"flag": true}'
                form["llm"]["provider_extra_body_json"] = '{"flag": true}'
                self.assertEqual(200, client.post("/api/config/preview", json=form).status_code)
                self.assertEqual(200, client.post("/api/config", json=form).status_code)
            saved = _load(path)
            self.assertEqual({"flag": 1}, saved["llm_local"]["provider_extra_body"])
            self.assertNotIn("provider_extra_body_json", saved["llm_local"])
            self.assertNotIn("provider_extra_body_raw", saved["llm_local"])

    def test_declared_raw_text_wins_and_malformed_text_is_controlled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW),
                               "llm_local": dict(LOW), "tts": {}, "generation": {}})
            app = FastAPI()
            app.include_router(system_module.router)
            client = TestClient(app, raise_server_exceptions=True)
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                form = _form(_get(tmp))
                for section in ("llm", "llm_local"):
                    form[section]["provider_extra_body"] = {"flag": 1}
                    form[section]["provider_extra_body_json"] = '{"flag": 2}'
                    form[section]["provider_extra_body_raw"] = True
                self.assertEqual(200, client.post("/api/config", json=form).status_code)
            self.assertEqual({"flag": 2}, _load(path)["llm_local"]["provider_extra_body"])

            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                form = _form(_get(tmp))
                for section in ("llm", "llm_local"):
                    form[section]["provider_extra_body"] = {"flag": 3}
                    form[section]["provider_extra_body_json"] = "{not json"
                    form[section]["provider_extra_body_raw"] = True
                self.assertEqual(400, client.post("/api/config", json=form).status_code)
            self.assertEqual({"flag": 2}, _load(path)["llm_local"]["provider_extra_body"])


class BooleanNumberReceiptTests(StalePredigestReceiptTests):
    def test_flag_true_receipt_cannot_vouch_for_flag_one_request(self):
        old = dict(LOW, provider_extra_body={"flag": True})
        new = dict(LOW, provider_extra_body={"flag": 1})
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(new, **self._receipt(old)),
                               "llm_local": dict(new), "tts": {}, "generation": {}})
            with patch.object(system_module, "CONFIG_PATH", path):
                proposal = system_module.get_config_to_save(
                    AppConfig.model_validate(_load(path)), _load(path))
            written = settings.get_document_with_retained_cache(proposal.model_dump(), _load(path))
            self.assertNotIn("concurrency", written["llm_local"])
            with _patches()[0], _patches()[1], patch.object(
                    llm_bench, "find_optimal_concurrency", side_effect=_find(3)) as bench:
                value = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    path, "local", new["base_url"], new["model_name"], object(),
                    status=STATUS, llm_config=new)
            bench.assert_called_once()
            self.assertEqual(3, value)


PAYLOAD_JS = r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(0,'utf8');
const input=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const el=()=>({value:'',checked:false});
const context={document:{getElementById:el}, llmProfiles:input.profiles, currentLlmMode:input.mode,
  promptPresetPayload:()=>({active:'default',own:[]}), passPromptDefaults:{pass1:{},pass3:{}},
  getPassPromptPresetPayload:()=>({active:'default',own:input.preset?[input.preset]:[]}),
  getNumFieldValue:(id,d)=>d, getOptionalTokenInput:()=>null, getIntListInput:()=>[],
  legacyChunkSize:3000, getDesignTextRangeInput:()=>({}), selectedPromptPreset:()=>null,
  getSynthesisWindowConfig:()=>({}), getSynthesisRecoveryConfig:()=>({}), getSpeechVerificationConfig:()=>({})};
vm.createContext(context); vm.runInContext(src,context);
const payload=vm.runInContext('buildConfigPayload(2)',context);
process.stdout.write(JSON.stringify({llm:payload.llm,llm_local:payload.llm_local,
                                     llm_remote:payload.llm_remote,llm_mode:payload.llm_mode,
                                     prompts:payload.prompts}));
"""


def _payload_via_js(tmp, document, mode, preset=None):
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "static/js/app-core.js"),
                  encoding="utf-8").read()
    start = source.index("        function buildConfigPayload(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    fixture = os.path.join(tmp, "payload-profiles.json")
    with open(fixture, "w", encoding="utf-8") as handle:
        json.dump({"profiles": {"local": document["llm_local"],
                                "remote": document["llm_remote"]}, "mode": mode,
                   "preset": preset}, handle)
    result = subprocess.run(["node", "-e", PAYLOAD_JS, fixture],
                            input=source[start:end], text=True, check=True,
                            capture_output=True, timeout=20)
    built = json.loads(result.stdout)
    prompts = {**(document.get("prompts") or {}), **built.pop("prompts", {})}
    return {**document, **built, "prompts": prompts}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class InactiveSlotExactBodyTests(unittest.TestCase):
    def test_unrelated_save_preserves_both_slots_exact_bodies(self):
        values = {"seed": 2 ** 53 + 1, "nested": {"big": 10 ** 310}}
        for mode in ("local", "remote"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                profile = dict(LOW, provider_extra_body=values)
                _seed(tmp, {"llm_mode": mode, "llm": dict(profile),
                            "llm_local": dict(profile), "llm_remote": dict(profile),
                            "tts": {}, "generation": {}})
                document = _get(tmp)
                document["llm_mode"] = "remote" if mode == "local" else "local"
                payload = _payload_via_js(tmp, document, document["llm_mode"])
                preview = _save_preview(tmp, payload)
                saved = _save(tmp, payload)
                self.assertEqual(200, preview)
                self.assertEqual([], [row for row in _get_preview_rows(tmp, payload)
                                      if "provider_extra_body" in row["path"]])
                for section in ("llm_local", "llm_remote"):
                    self.assertEqual(values, saved[section]["provider_extra_body"], section)


def _get_preview_rows(tmp, document):
    with patch.object(system_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
        return asyncio.run(system_module.preview_config(document))["changes"]


class ProviderBodyValidationTests(unittest.TestCase):
    def test_nonfinite_raw_and_structured_values_are_refused(self):
        for raw in ('{"exp":1e310}', '{"exp":NaN}', '{"nested":{"v":Infinity}}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW),
                                   "llm_local": dict(LOW), "tts": {}, "generation": {}})
                before = _load(path)
                app = FastAPI()
                app.include_router(system_module.router)
                client = TestClient(app, raise_server_exceptions=True)
                with patch.object(system_module, "CONFIG_PATH", path), \
                        patch.object(system_module.project_manager, "engine", None), \
                        patch.object(system_module.project_manager, "invalidate_config_cache"):
                    form = _form(_get(tmp))
                    for section in ("llm", "llm_local"):
                        form[section]["provider_extra_body_json"] = raw
                        form[section]["provider_extra_body_raw"] = True
                    self.assertIn(_post_unsafe(client, "/api/config/preview", form).status_code,
                                  (400, 422))
                    self.assertIn(_post_unsafe(client, "/api/config", form).status_code, (400, 422))
                    self.assertEqual(200, client.get("/api/config").status_code)
                self.assertEqual(before, _load(path))

    def test_malformed_authoritative_raw_is_refused_and_bytes_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = {"seed": 9007199254740993}
            profile = dict(LOW, provider_extra_body=body)
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                               "llm_local": dict(profile), "tts": {}, "generation": {}})
            before = _load(path)
            app = FastAPI()
            app.include_router(system_module.router)
            client = TestClient(app, raise_server_exceptions=True)
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                form = _form(_get(tmp))
                for section in ("llm", "llm_local"):
                    form[section].pop("provider_extra_body", None)
                    form[section]["provider_extra_body_json"] = "{bad"
                    form[section]["provider_extra_body_raw"] = True
                self.assertEqual(400, client.post("/api/config/preview", json=form).status_code)
                self.assertEqual(400, client.post("/api/config", json=form).status_code)
            self.assertEqual(before, _load(path))

    def test_preview_shows_json_type_change_but_not_numeric_equivalence(self):
        from config_settings import AppConfig, load_app_config_result
        for old, new, expected in ((True, 1, 1), (1, 1.0, 0)):
            with self.subTest(old=old, new=new), tempfile.TemporaryDirectory() as tmp:
                path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW, provider_extra_body={"flag": old}),
                                   "llm_local": dict(LOW, provider_extra_body={"flag": old}),
                                   "tts": {}, "generation": {}})
                existing = load_app_config_result(path)
                document = existing.data.copy()
                document["llm"] = document["llm_local"] = dict(LOW, provider_extra_body={"flag": new})
                preview = config_change_preview.get_config_change_preview(
                    existing, AppConfig.model_validate(document),
                    system_module.get_config_local_profile, exists=True)
                rows = [row for row in preview["changes"]
                        if row["path"] == "llm_local.provider_extra_body"]
                self.assertEqual(expected, len(rows))
                if expected:
                    self.assertEqual("object[boolean]", rows[0]["before"])
                    self.assertEqual("object[number]", rows[0]["after"])
                    self.assertNotIn("flag", rows[0]["before"] + rows[0]["after"])


class StructuredNonfiniteTests(unittest.TestCase):
    def test_structured_only_nonfinite_is_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW),
                               "llm_local": dict(LOW), "tts": {}, "generation": {}})
            before = _load(path)
            app = FastAPI()
            app.include_router(system_module.router)
            client = TestClient(app, raise_server_exceptions=True)
            with patch.object(system_module, "CONFIG_PATH", path), \
                    patch.object(system_module.project_manager, "engine", None), \
                    patch.object(system_module.project_manager, "invalidate_config_cache"):
                form = _form(_get(tmp))
                for section in ("llm", "llm_local"):
                    form[section]["provider_extra_body"] = {"nested": {"exp": 1e310}}
                self.assertIn(_post_unsafe(client, "/api/config/preview", form).status_code,
                              (400, 422))
                self.assertIn(_post_unsafe(client, "/api/config", form).status_code, (400, 422))
                self.assertEqual(200, client.get("/api/config").status_code)
            self.assertEqual(before, _load(path))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class InlinePresetPayloadTests(unittest.TestCase):
    def test_inline_pass1_edit_survives_production_payload_and_save(self):
        preset = {"name": "fixture-inline", "user_prompt": "EDITED_PASS1_TEXT"}
        with tempfile.TemporaryDirectory() as tmp:
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(LOW), "llm_local": dict(LOW),
                               "tts": {}, "generation": {},
                               "prompts": {"system_prompt": "", "user_prompt": "",
                                           "pass1_preset": "fixture-inline",
                                           "pass1_prompt_presets": []}})
            document = _get(tmp)
            payload = _payload_via_js(tmp, document, "local", preset=preset)
            self.assertIn(preset, payload["prompts"]["pass1_prompt_presets"])
            self.assertEqual(200, _save_preview(tmp, payload))
            saved = _save(tmp, payload)
            self.assertTrue(any(item.get("name") == preset["name"]
                                and item.get("user_prompt") == preset["user_prompt"]
                                for item in saved["prompts"]["pass1_prompt_presets"]))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BlankBodyTextareaTests(unittest.TestCase):
    def test_blank_textarea_submits_explicit_empty_object(self):
        blank_js = FORM_JS.replace(
            'populateLlmInputs("local"); getEditedLlmProfile();',
            'populateLlmInputs("local"); document.getElementById("llm-provider-extra-body").value="   "; getEditedLlmProfile();')
        with tempfile.TemporaryDirectory() as tmp:
            body = {"fixture": 1}
            profile = dict(LOW, provider_extra_body=body)
            _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                        "llm_local": dict(profile), "tts": {}, "generation": {}})
            document = _get(tmp)
            result = subprocess.run(["node", "-e", blank_js],
                                    input=json.dumps({"local": document["llm_local"]}),
                                    text=True, check=True, capture_output=True, timeout=20)
            posted = json.loads(result.stdout)
            self.assertEqual("{}", posted["provider_extra_body_json"])
            form = {**_form(document), "llm": posted, "llm_local": posted}
            self.assertEqual(200, _save_preview(tmp, form))
            self.assertEqual({}, _save(tmp, form)["llm_local"]["provider_extra_body"])


class PreviewBaselineAndTypeTests(unittest.TestCase):
    def test_hydrated_baseline_and_nested_type_summary(self):
        from config_settings import AppConfig, load_app_config_result
        for mode in ("local", "remote"):
            body = {"nested": {"flag": True}, "big": 2 ** 53 + 1}
            profile = dict(LOW, provider_extra_body=body)
            active = f"llm_{mode}"
            neutral = LLMConfig(base_url="", api_key="", model_name="").model_dump()
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                path = _seed(tmp, {"llm_mode": mode, "llm": dict(profile),
                                   active: dict(neutral), "tts": {}, "generation": {}})
                existing = load_app_config_result(path)
                document = _get(tmp)
                unchanged = config_change_preview.get_config_change_preview(
                    existing, AppConfig.model_validate(document),
                    system_module.get_config_local_profile, exists=True)
                self.assertEqual([], [row for row in unchanged["changes"]
                                      if "provider_extra_body" in row["path"]])

                for flag, expected_rows in ((1, 1), (1.0, 1)):
                    edited = json.loads(json.dumps(document))
                    edited[active]["provider_extra_body"] = {"nested": {"flag": flag},
                                                              "big": 2 ** 53 + 1}
                    preview = config_change_preview.get_config_change_preview(
                        existing, AppConfig.model_validate(edited),
                        system_module.get_config_local_profile, exists=True)
                    rows = [row for row in preview["changes"]
                            if row["path"] == active + ".provider_extra_body"]
                    self.assertEqual(expected_rows, len(rows))
                    if expected_rows == 0:
                        continue
                    text = rows[0]["before"] + rows[0]["after"]
                    self.assertIn("boolean", rows[0]["before"])
                    self.assertIn("number", rows[0]["after"])
                    self.assertNotIn("flag", text)
                    self.assertNotIn("9007199254740993", text)

    def test_numeric_equivalence_keeps_no_row(self):
        from config_settings import AppConfig, load_app_config_result
        with tempfile.TemporaryDirectory() as tmp:
            profile = dict(LOW, provider_extra_body={"nested": {"flag": 1}})
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                               "llm_local": dict(profile), "tts": {}, "generation": {}})
            existing = load_app_config_result(path)
            document = _get(tmp)
            document["llm_local"]["provider_extra_body"] = {"nested": {"flag": 1.0}}
            preview = config_change_preview.get_config_change_preview(
                existing, AppConfig.model_validate(document),
                system_module.get_config_local_profile, exists=True)
            self.assertEqual([], [row for row in preview["changes"]
                                  if row["path"] == "llm_local.provider_extra_body"])


class SaveRedactionTests(unittest.TestCase):
    def test_invalid_provider_json_never_echoes_submitted_fields(self):
        secret = "SYNTHETIC_SECRET_KEY_ZZZ"
        auth = "SYNTHETIC_AUTH_HEADER_QQQ"
        raw = '{"deep":{"nested":[{"exp":1e310}]}}'
        for variant in ("raw", "structured", "stale_aux"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                profile = dict(LOW, api_key="saved-legacy",
                               provider_headers={"authorization": "saved-header"})
                path = _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                                   "llm_local": dict(profile), "tts": {}, "generation": {}})
                before = _load(path)
                app = FastAPI()
                app.include_router(system_module.router)
                client = TestClient(app, raise_server_exceptions=True)
                with patch.object(system_module, "CONFIG_PATH", path), \
                        patch.object(system_module.project_manager, "engine", None), \
                        patch.object(system_module.project_manager, "invalidate_config_cache"):
                    form = _form(_get(tmp))
                    for section in ("llm", "llm_local"):
                        form[section]["api_key"] = secret
                        form[section]["provider_headers"] = {"authorization": auth}
                        if variant == "raw":
                            form[section]["provider_extra_body_json"] = raw
                            form[section]["provider_extra_body_raw"] = True
                        elif variant == "structured":
                            form[section]["provider_extra_body"] = {"deep": {"v": 1e310}}
                        else:
                            form[section]["provider_extra_body"] = {"valid": 1}
                            form[section]["provider_extra_body_json"] = raw
                    preview = _post_unsafe(client, "/api/config/preview", form)
                    saved = _post_unsafe(client, "/api/config", form)
                    self.assertEqual(200, client.get("/api/config").status_code)
                text = preview.text + saved.text
                for value in (secret, auth, "deep", "1e310"):
                    self.assertNotIn(value, text)
                if variant == "stale_aux":
                    self.assertEqual(200, preview.status_code)
                else:
                    self.assertIn(preview.status_code, (400, 422))
                self.assertIn(saved.status_code, (400, 422))
                self.assertEqual(before, _load(path))


class SharedHydrationBaselineTests(unittest.TestCase):
    def test_baseline_follows_get_hydration_source_in_both_modes(self):
        from config_settings import AppConfig, load_app_config_result
        legacy_body = {"nested": {"flag": True}, "big": 2 ** 53 + 1}
        for mode in ("local", "remote"):
            active = f"llm_{mode}"
            inactive = "llm_remote" if mode == "local" else "llm_local"
            legacy = dict(LOW, provider_extra_body=legacy_body, api_key="legacy-fixture")
            neutral = LLMConfig(base_url="", api_key="", model_name="").model_dump()
            for case in ("active_omitted", "inactive_empty", "inactive_edit"):
                with self.subTest(mode=mode, case=case), tempfile.TemporaryDirectory() as tmp:
                    saved_inactive = dict(LOW, provider_extra_body={}, api_key="inactive-fixture",
                                          model_name="other-model")
                    path = _seed(tmp, {"llm_mode": mode, "llm": dict(legacy),
                                       active: dict(neutral), inactive: saved_inactive,
                                       "tts": {}, "generation": {}})
                    existing = load_app_config_result(path)
                    document = _get(tmp)
                    if case == "inactive_edit":
                        document[inactive]["provider_extra_body"] = legacy_body
                    preview = config_change_preview.get_config_change_preview(
                        existing, AppConfig.model_validate(document),
                        system_module.get_config_local_profile, exists=True)
                    rows = [row for row in preview["changes"]
                            if "provider_extra_body" in row["path"]]
                    if case == "inactive_edit":
                        self.assertEqual([inactive + ".provider_extra_body"],
                                         [row["path"] for row in rows])
                    else:
                        self.assertEqual([], rows)


class PositionalTypeSignatureTests(unittest.TestCase):
    def test_identical_aggregate_types_still_show_a_leaf_change(self):
        from config_change_preview import _json_types
        for before, after in (({"x": [True, 1]}, {"x": [1, 1]}),
                              ({"x": 1, "y": True}, {"x": 1, "y": 1}),
                              ({"rows": [{"v": True}, {"v": 1}]},
                               {"rows": [{"v": 1}, {"v": 1}]})):
            self.assertNotEqual(_json_types(before), _json_types(after))
            self.assertEqual(_json_types(before), _json_types(json.loads(json.dumps(before))))
        self.assertEqual(_json_types({"v": 1}), _json_types({"v": 1.0}))
        large = {"v": list(range(50))}
        self.assertLessEqual(len(_json_types(large)), 120)

    def test_preview_position_signature_for_nested_leaf_swap(self):
        from config_settings import AppConfig, load_app_config_result
        with tempfile.TemporaryDirectory() as tmp:
            profile = dict(LOW, provider_extra_body={"rows": [{"v": True}, {"v": 1}]})
            path = _seed(tmp, {"llm_mode": "local", "llm": dict(profile),
                               "llm_local": dict(profile), "tts": {}, "generation": {}})
            existing = load_app_config_result(path)
            document = _get(tmp)
            document["llm_local"]["provider_extra_body"] = {"rows": [{"v": 1}, {"v": 1}]}
            preview = config_change_preview.get_config_change_preview(
                existing, AppConfig.model_validate(document),
                system_module.get_config_local_profile, exists=True)
            rows = [row for row in preview["changes"]
                    if row["path"] == "llm_local.provider_extra_body"]
            self.assertEqual(1, len(rows))
            self.assertNotEqual(rows[0]["before"], rows[0]["after"])
            self.assertNotIn("rows", rows[0]["before"] + rows[0]["after"])
            self.assertNotIn("v", rows[0]["before"] + rows[0]["after"])


if __name__ == "__main__":
    unittest.main()
