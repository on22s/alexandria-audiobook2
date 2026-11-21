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


if __name__ == "__main__":
    unittest.main()
