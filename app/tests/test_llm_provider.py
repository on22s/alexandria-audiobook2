import json
import time
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI
from pydantic import ValidationError

from config_settings import LLMConfig
import llm_provider
from llm_provider import (ConfiguredOpenAI, MissingAPIKeyError, get_profile_timeout, make_llm_client,
                          merge_provider_extra_body, classify_llm_error, get_retry_delay,
                          is_failover_eligible, is_official_openai_endpoint, resolve_api_key,
                          resolve_client_api_key)


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "response"


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = _FakeChat()
        _FakeOpenAI.instances.append(self)


class _PacedClient:
    def __init__(self):
        self.chat = _FakeChat()

    def with_options(self, **kwargs):
        return self


class ProviderRequestSettingsTest(unittest.TestCase):
    def setUp(self):
        _FakeOpenAI.instances.clear()

    def test_context_rejections_are_distinct_from_other_bad_requests(self):
        from types import SimpleNamespace
        for body in ({'code': 'context_length_exceeded'},
                     {'error': {'code': 'context_length_exceeded'}}):
            error = SimpleNamespace(status_code=400, body=body)
            result = classify_llm_error(error)
            self.assertEqual(result['category'], 'context_budget')
            self.assertFalse(result['retryable'])
        error = SimpleNamespace(status_code=400, body={'error': {'code': 'invalid_api_key'}})
        result = classify_llm_error(error)
        self.assertEqual(result['category'], 'api_error')
        self.assertFalse(result['retryable'],
                         'a deterministic 400 must not burn retries like a context rejection')

    def test_profile_keeps_json_provider_options(self):
        profile = LLMConfig(
            base_url="http://localhost:1234/v1", api_key="key", model_name="model",
            provider_headers={"X-Tenant": "reader"},
            provider_extra_body={"reasoning_effort": "none", "nested": {"mode": "fast"}},
        )
        self.assertEqual("reader", profile.provider_headers["X-Tenant"])
        self.assertEqual("fast", profile.provider_extra_body["nested"]["mode"])

    def test_profile_rejects_invalid_timing_values(self):
        base = {"base_url": "http://localhost:1234/v1", "api_key": "key", "model_name": "model"}
        for field, value in (("request_timeout_seconds", 0),
                             ("connect_timeout_seconds", 301),
                             ("request_interval_seconds", -0.1),
                             ("api_retry_limit", 11),
                             ("retry_initial_delay_seconds", -0.1),
                             ("retry_multiplier", 0.9),
                             ("retry_max_delay_seconds", 301)):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    LLMConfig(**base, **{field: value})

    def test_profile_timeout_uses_request_and_connect_limits(self):
        timeout = get_profile_timeout(
            {"request_timeout_seconds": 45, "connect_timeout_seconds": 3}, 600)
        # openai 3 sends through httpx2; its own Timeout type is what it documents.
        self.assertIsInstance(timeout, httpx2.Timeout)
        self.assertEqual(45, timeout.read)
        self.assertEqual(3, timeout.connect)

    def test_error_classification_and_backoff_are_deterministic(self):
        rate_limited = type("RateLimited", (Exception,), {"status_code": 429})("slow down")
        policy = Exception("blocked by content policy")
        self.assertEqual("rate_limited", classify_llm_error(rate_limited)["category"])
        self.assertTrue(classify_llm_error(rate_limited)["retryable"])
        self.assertEqual("content_policy", classify_llm_error(policy)["category"])
        self.assertFalse(classify_llm_error(policy)["retryable"])
        self.assertEqual(4, get_retry_delay(1, 2, 10, 3))
        self.assertEqual(10, get_retry_delay(1, 2, 10, 8))

    def test_retry_jitter_stays_inside_its_band_and_under_the_cap(self):
        import random
        rng = random.Random(7)
        seen = {get_retry_delay(1, 2, 10, 3, jitter=0.25, rng=rng) for _ in range(200)}
        self.assertTrue(all(3.0 <= d <= 5.0 for d in seen), sorted(seen)[:3] + sorted(seen)[-3:])
        self.assertGreater(len(seen), 50)  # it does vary
        self.assertTrue(all(get_retry_delay(1, 2, 10, 8, jitter=0.5, rng=rng) <= 10 for _ in range(50)))
        self.assertEqual(4, get_retry_delay(1, 2, 10, 3, jitter=0, rng=rng))

    def test_explicit_generation_options_override_provider_defaults(self):
        merged = merge_provider_extra_body(
            {"reasoning_effort": "high", "provider_flag": True},
            {"reasoning_effort": "none", "top_k": 20},
        )
        self.assertEqual(
            {"reasoning_effort": "none", "provider_flag": True, "top_k": 20}, merged)

    def test_client_sends_profile_headers_and_body_on_completions(self):
        profile = {
            "base_url": "http://localhost:1234/v1", "api_key": "key",
            "provider_headers": {"X-Tenant": "reader"},
            "provider_extra_body": {"reasoning_effort": "high", "provider_flag": True},
        }
        with patch("llm_provider.OpenAI", _FakeOpenAI):
            client = make_llm_client(profile, timeout=30)
            result = client.chat.completions.create(
                model="model", messages=[], extra_body={"reasoning_effort": "none"})

        self.assertEqual("response", result)
        raw = _FakeOpenAI.instances[0]
        self.assertEqual({"X-Tenant": "reader"}, raw.kwargs["default_headers"])
        _, kwargs = raw.chat.completions.calls[0]
        self.assertEqual(
            {"reasoning_effort": "none", "provider_flag": True}, kwargs["extra_body"])

    def test_profile_reasoning_effort_reaches_every_completion(self):
        from llm_provider import get_provider_extra_body
        profile = {"base_url": "http://localhost:1234/v1", "api_key": "key",
                   "reasoning_effort": "low"}
        self.assertEqual({"reasoning_effort": "low"}, get_provider_extra_body(profile))
        with patch("llm_provider.OpenAI", _FakeOpenAI):
            client = make_llm_client(profile, timeout=30)
            client.chat.completions.create(model="model", messages=[])       # a raw caller
            client.chat.completions.create(model="model", messages=[],       # a run's own choice
                                           extra_body={"reasoning_effort": "none"})
        calls = _FakeOpenAI.instances[-1].chat.completions.calls
        self.assertEqual({"reasoning_effort": "low"}, calls[0][1]["extra_body"])
        self.assertEqual({"reasoning_effort": "none"}, calls[1][1]["extra_body"])
        # the custom request-body JSON is the escape hatch and beats the dropdown
        self.assertEqual({"reasoning_effort": "xhigh"}, get_provider_extra_body(
            {"reasoning_effort": "low", "provider_extra_body": {"reasoning_effort": "xhigh"}}))
        self.assertEqual({}, get_provider_extra_body({"reasoning_effort": None}))

    def test_reasoning_effort_setting_is_validated(self):
        from config_settings import LLMConfig
        base = {"base_url": "http://x/v1", "api_key": "k", "model_name": "m"}
        self.assertEqual("none", LLMConfig(**base, reasoning_effort="none").reasoning_effort)
        self.assertIsNone(LLMConfig(**base).reasoning_effort)
        with self.assertRaises(Exception):
            LLMConfig(**base, reasoning_effort="max")

    def _configured_client_with_capture(self, provider_extra_body):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": "model", "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            })

        raw_client = OpenAI(
            base_url="http://provider.test/v1", api_key="key",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
        )
        self.addCleanup(raw_client.close)
        return ConfiguredOpenAI(raw_client, provider_extra_body), requests

    def test_formal_completion_arguments_override_provider_body_defaults(self):
        client, requests = self._configured_client_with_capture(
            {"temperature": 1.5, "top_p": 0.2, "provider_flag": True})

        client.chat.completions.create(
            model="model", messages=[{"role": "user", "content": "hello"}],
            temperature=0.1, top_p=0.9)

        self.assertEqual(0.1, requests[0]["temperature"])
        self.assertEqual(0.9, requests[0]["top_p"])
        self.assertTrue(requests[0]["provider_flag"])

    def test_with_options_retains_provider_body_defaults(self):
        client, requests = self._configured_client_with_capture({"provider_flag": True})

        client.with_options(timeout=1).chat.completions.create(
            model="model", messages=[{"role": "user", "content": "hello"}])

        self.assertTrue(requests[0]["provider_flag"])

    def test_request_interval_applies_across_with_options_clients(self):
        raw_client = _PacedClient()
        client = ConfiguredOpenAI(raw_client, {}, request_interval_seconds=0.03)

        client.chat.completions.create(model="model", messages=[])
        first = time.monotonic()
        client.with_options(timeout=1).chat.completions.create(model="model", messages=[])
        second = time.monotonic()

        self.assertGreaterEqual(second - first, 0.02)




class ReasoningModelRequestShapeTests(unittest.TestCase):
    """OpenAI's GPT-5 / o-series reject `max_tokens` (they want
    `max_completion_tokens`) and, with reasoning on, the sampling controls.
    Local servers and non-reasoning models must keep every parameter."""

    def _create(self, profile, **kwargs):
        with patch("llm_provider.OpenAI", _FakeOpenAI):
            _FakeOpenAI.instances.clear()
            client = make_llm_client(profile, timeout=5)
            client.chat.completions.create(**kwargs)
        return _FakeOpenAI.instances[-1].chat.completions.calls[-1][1]

    def test_local_server_request_is_untouched(self):
        sent = self._create({"base_url": "http://127.0.0.1:8090/v1", "model_name": "qwen3-14b",
                             "reasoning_effort": "medium"},
                            model="qwen3-14b", max_tokens=4096, temperature=0.1, top_p=0.9, messages=[])
        self.assertEqual(4096, sent["max_tokens"])
        self.assertEqual(0.1, sent["temperature"])
        self.assertEqual({"reasoning_effort": "medium"}, sent["extra_body"])

    def test_gpt5_with_reasoning_gets_completion_tokens_and_no_sampling(self):
        sent = self._create({"base_url": "https://api.openai.com/v1", "model_name": "gpt-5.6",
                             "reasoning_effort": "medium"},
                            model="gpt-5.6", max_tokens=4096, temperature=0.1, top_p=0.9, messages=[])
        self.assertEqual(4096, sent["max_completion_tokens"])
        self.assertNotIn("max_tokens", sent)
        self.assertNotIn("temperature", sent)
        self.assertNotIn("top_p", sent)
        self.assertEqual("medium", sent["extra_body"]["reasoning_effort"])

    def test_explicit_completion_budget_wins_and_obsolete_limit_is_removed(self):
        from llm_provider import adapt_request_for_reasoning_model
        original = {"model": "o3", "messages": [], "max_tokens": 4096,
                    "max_completion_tokens": 1024}
        sent = self._create({"base_url": "https://api.openai.com/v1"}, **original)
        self.assertNotIn("max_tokens", sent)
        self.assertEqual(1024, sent["max_completion_tokens"])
        adapted = adapt_request_for_reasoning_model(original)
        self.assertNotIn("max_tokens", adapted)
        self.assertEqual(1024, adapted["max_completion_tokens"])
        self.assertEqual(4096, original["max_tokens"])
        self.assertEqual(1024, original["max_completion_tokens"])
        local = self._create({"base_url": "http://localhost:8090/v1"},
                             **dict(original, model="qwen3-14b"))
        self.assertNotIn("max_tokens", local)
        self.assertEqual(1024, local["max_completion_tokens"])

    def test_omitted_effort_drops_sampling_for_reasoning_defaults(self):
        for model in ("gpt-5", "gpt-5-mini", "gpt-5.5", "gpt-5.6", "o3", "o4-mini"):
            with self.subTest(model=model):
                sent = self._create({"base_url": "https://api.openai.com/v1"},
                                    model=model, max_tokens=64, temperature=0.1,
                                    top_p=0.9, presence_penalty=0.2,
                                    frequency_penalty=0.3, messages=[])
                for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                    self.assertNotIn(key, sent)
                self.assertEqual(64, sent["max_completion_tokens"])
                self.assertNotIn("reasoning_effort", sent["extra_body"])

    def test_omitted_effort_preserves_sampling_for_documented_none_defaults(self):
        for model in ("gpt-5.1", "gpt-5.1-2025-11-13", "gpt-5.2",
                      "gpt-5.2-2025-12-11", "gpt-5.4", "gpt-5.4-2026-03-05"):
            with self.subTest(model=model):
                sent = self._create({"base_url": "https://api.openai.com/v1"},
                                    model=model, temperature=0.2, top_p=0.8, messages=[])
                self.assertEqual(0.2, sent["temperature"])
                self.assertEqual(0.8, sent["top_p"])
                self.assertNotIn("reasoning_effort", sent["extra_body"])

    def test_top_level_effort_and_extra_body_override_control_sampling(self):
        for effort, extra, keep in (("high", {}, False), ("none", {}, True),
                                    ("high", {"reasoning_effort": "none"}, True),
                                    ("none", {"reasoning_effort": "high"}, False)):
            with self.subTest(effort=effort, extra=extra):
                sent = self._create({"base_url": "https://api.openai.com/v1",
                                     "reasoning_effort": "medium"},
                                    model="gpt-5.1", reasoning_effort=effort,
                                    extra_body=extra, temperature=0.2, messages=[])
                self.assertEqual(keep, "temperature" in sent)
                self.assertEqual(effort, sent["reasoning_effort"])
                self.assertEqual(extra, sent["extra_body"])

    def test_gpt5_with_reasoning_off_keeps_sampling(self):
        sent = self._create({"base_url": "https://api.openai.com/v1", "model_name": "gpt-5.6",
                             "reasoning_effort": "none"},
                            model="gpt-5.6", max_tokens=64, temperature=0.1, messages=[])
        self.assertEqual(64, sent["max_completion_tokens"])
        self.assertEqual(0.1, sent["temperature"])


class ApiKeyReferenceTests(unittest.TestCase):
    def test_env_references_resolve_and_literals_pass_through(self):
        from llm_provider import is_api_key_reference
        openai = "https://api.openai.com/v1"
        local = "http://localhost:1234/v1"
        with patch.dict("os.environ", {"MY_KEY": "sk-from-env", "OPENAI_API_KEY": "sk-default"}, clear=False):
            self.assertEqual("sk-from-env", resolve_api_key("env:MY_KEY", openai))
            self.assertEqual("sk-from-env", resolve_api_key("${MY_KEY}", openai))
            self.assertEqual("sk-default", resolve_api_key("", openai))
            self.assertEqual("local", resolve_api_key("local", openai))
            self.assertEqual("sk-literal", resolve_api_key("sk-literal", openai))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual("", resolve_api_key("env:MISSING", openai), "a typo must not become the key")
            self.assertEqual("local", resolve_api_key("", local))
        self.assertTrue(is_api_key_reference("env:MY_KEY"))
        self.assertTrue(is_api_key_reference("${MY_KEY}"))
        self.assertFalse(is_api_key_reference("sk-literal"))

    def test_implicit_openai_key_is_scoped_to_the_official_endpoint(self):
        openai = "https://api.openai.com/v1"
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-real-openai"}, clear=False):
            for url in (openai, "https://api.openai.com", "https://API.OPENAI.COM./v1"):
                with self.subTest(allowed=url):
                    self.assertTrue(is_official_openai_endpoint(url))
                    self.assertEqual("sk-real-openai", resolve_api_key("", url))
            third_party = ("https://api.deepseek.com/v1", "https://openrouter.ai/api/v1",
                           "https://instance.thundercompute.net:1234/v1", "http://10.0.0.9:1234/v1",
                           "https://api.openai.com.evil.example/v1", "http://api.openai.com/v1",
                           "https://api.openai.com:8443/v1", "https://user@api.openai.com/v1",
                           "https://api.openai.com/v1?redirect=evil")
            for url in third_party:
                with self.subTest(refused=url):
                    self.assertFalse(is_official_openai_endpoint(url))
                    with self.assertRaises(MissingAPIKeyError) as raised:
                        resolve_api_key("", url)
                    self.assertNotIn("sk-real-openai", str(raised.exception))
            for url in ("http://localhost:1234/v1", "http://127.0.0.1:1234/v1", "", None):
                with self.subTest(local=url):
                    self.assertEqual("local", resolve_api_key("", url))

    def test_explicit_keys_are_preserved_on_every_endpoint(self):
        with patch.dict("os.environ", {"MY_KEY": "sk-from-env", "OPENAI_API_KEY": "sk-real-openai"},
                        clear=False):
            for url in ("https://api.deepseek.com/v1", "http://localhost:1234/v1", ""):
                with self.subTest(url=url):
                    self.assertEqual("sk-from-env", resolve_api_key("env:MY_KEY", url))
                    self.assertEqual("sk-from-env", resolve_api_key("${MY_KEY}", url))
                    self.assertEqual("sk-explicit", resolve_api_key("sk-explicit", url))
            self.assertEqual("", resolve_api_key("env:MISSING", "https://api.deepseek.com/v1"),
                             "a typo must surface as an auth failure, not a fallback key")

    def test_client_never_sends_the_implicit_openai_key_to_a_third_party(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-real-openai"}, clear=False):
            with patch("llm_provider.OpenAI", _FakeOpenAI):
                _FakeOpenAI.instances.clear()
                with self.assertRaises(MissingAPIKeyError):
                    make_llm_client({"base_url": "https://api.deepseek.com/v1", "api_key": ""}, timeout=5)
            self.assertEqual([], _FakeOpenAI.instances, "no client may be built with the wrong key")
            with patch("llm_provider.OpenAI", _FakeOpenAI):
                make_llm_client({"base_url": "https://api.openai.com/v1", "api_key": ""}, timeout=5)
            self.assertEqual("sk-real-openai", _FakeOpenAI.instances[-1].kwargs["api_key"])
            with patch("llm_provider.OpenAI", _FakeOpenAI):
                make_llm_client({"base_url": "http://localhost:1234/v1", "api_key": ""}, timeout=5)
            self.assertEqual("local", _FakeOpenAI.instances[-1].kwargs["api_key"])

    def test_auth_and_not_found_stop_retrying_but_throttling_does_not(self):
        for status, category in ((401, "auth"), (403, "auth"), (404, "not_found")):
            with self.subTest(status=status):
                error = type("ProviderError", (Exception,), {"status_code": status})(f"HTTP {status}")
                result = classify_llm_error(error)
                self.assertEqual(category, result["category"])
                self.assertFalse(result["retryable"])
        for status, category in ((429, "rate_limited"), (503, "server_error")):
            with self.subTest(status=status):
                error = type("ProviderError", (Exception,), {"status_code": status})(f"HTTP {status}")
                result = classify_llm_error(error)
                self.assertEqual(category, result["category"])
                self.assertTrue(result["retryable"])

    def test_failover_eligibility_keeps_provider_errors_and_stops_local_ones(self):
        for category in ("content_policy", "auth", "not_found", "rate_limited", "server_error",
                         "timeout", "connection_error", "api_error"):
            with self.subTest(eligible=category):
                self.assertTrue(is_failover_eligible({"category": category}))
        for category in ("context_budget", "profile_changed", "request_admission"):
            with self.subTest(local=category):
                self.assertFalse(is_failover_eligible({"category": category}))

    def test_client_is_built_with_the_resolved_key(self):
        with patch("llm_provider.OpenAI", _FakeOpenAI), \
                patch.dict("os.environ", {"MY_KEY": "sk-from-env"}, clear=False):
            _FakeOpenAI.instances.clear()
            make_llm_client({"base_url": "https://api.openai.com/v1", "api_key": "env:MY_KEY"}, timeout=5)
        self.assertEqual("sk-from-env", _FakeOpenAI.instances[-1].kwargs["api_key"])

    def test_setup_page_shows_a_reference_but_redacts_a_secret(self):
        from routers.system import _redact_config_secrets, _REDACTED_SECRET
        safe = _redact_config_secrets({"llm": {"api_key": "env:MY_KEY"}, "llm_remote": {"api_key": "sk-literal"}})
        self.assertEqual("env:MY_KEY", safe["llm"]["api_key"])
        self.assertEqual(_REDACTED_SECRET, safe["llm_remote"]["api_key"])
        self.assertTrue(safe["llm"]["api_key_configured"])


class CredentialProfilePolicyTests(unittest.TestCase):
    """Wire-level checks for the effective client profile (offline, synthetic)."""

    def _sdk_factory(self, handler, seen):
        policy_client = llm_provider.OpenAI

        def factory(**kwargs):
            client = policy_client(
                **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
            seen.append(client)
            return client
        return factory

    @staticmethod
    def _completion():
        return httpx2.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}]})

    def test_hosted_authorization_header_profile_sends_only_the_configured_bearer(self):
        seen, clients = [], []

        def handler(request):
            seen.append((request.headers.get("authorization"),
                         [value for name, value in request.headers.items()
                          if name.lower() == "authorization"]))
            return self._completion()

        profile = {"base_url": "https://hosted.invalid/v1", "api_key": "",
                   "provider_headers": {"authorization": "Bearer configured-fixture"}}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-synthetic"}, clear=False), \
                patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
            client = make_llm_client(profile, timeout=5)
            client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(1, len(clients))
        self.assertEqual("local", clients[0].api_key, "the SDK placeholder must be harmless")
        self.assertEqual("Bearer configured-fixture", seen[0][0])
        self.assertEqual(["Bearer configured-fixture"], seen[0][1], "only the configured header")
        self.assertNotIn("sk-synthetic", str(seen))

    def test_effective_key_preserves_omitted_blank_manual_and_header_profiles(self):
        openai = "https://api.openai.com/v1"
        manual = {"transport": "manual", "base_url": "https://hosted.invalid/v1"}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-synthetic"}, clear=False):
            self.assertEqual("manual", resolve_client_api_key(manual))
            self.assertEqual("local", resolve_client_api_key(
                {"base_url": "https://hosted.invalid/v1"}), "omitted keys keep the legacy placeholder")
            self.assertEqual("sk-synthetic", resolve_client_api_key(
                {"base_url": openai, "api_key": ""}), "a blank official-OpenAI key inherits the env key")
            self.assertEqual("local", resolve_client_api_key(
                {"base_url": "https://hosted.invalid/v1", "api_key": "",
                 "provider_headers": {"AUTHORIZATION": "Bearer configured-fixture"}}))

    def test_sdk_retries_are_terminal_for_deterministic_statuses_only(self):
        for status, expected in ((401, 1), (403, 1), (404, 1), (429, 3), (503, 3)):
            with self.subTest(status=status):
                calls, clients = [], []

                def handler(request, status=status):
                    calls.append(request)
                    return httpx2.Response(status, headers={"x-should-retry": "true"}, json={
                        "error": {"message": "fixture", "type": "invalid_request_error"}})

                with patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
                    client = make_llm_client({"base_url": "https://hosted.invalid/v1",
                                              "api_key": "fixture"}, timeout=5)
                    with self.assertRaises(Exception):
                        client.chat.completions.create(
                            model="m", messages=[{"role": "user", "content": "hi"}])
                self.assertEqual(expected, len(calls),
                                 "401/403/404 must be terminal; 429/503 keep transient recovery")

    def test_sdk_transport_keeps_timeout_recovery(self):
        calls, clients = [], []

        def handler(request):
            calls.append(request)
            raise httpx2.ConnectTimeout("offline fixture")

        with patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
            client = make_llm_client({"base_url": "https://hosted.invalid/v1",
                                      "api_key": "fixture"}, timeout=5)
            with self.assertRaises(Exception):
                client.chat.completions.create(
                    model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertGreater(len(calls), 1, "timeouts must keep the SDK's transient recovery")

    def test_canonical_openai_urls_are_accepted_by_validation_and_resolution(self):
        from core import LLMConfigError, _validate_local_llm_base_url
        accepted = ("https://api.openai.com/v1", "https://api.openai.com",
                    "https://API.OPENAI.COM./v1", "https://api.openai.com:443/v1")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-synthetic"}, clear=False):
            for url in accepted:
                with self.subTest(accepted=url):
                    self.assertTrue(is_official_openai_endpoint(url))
                    self.assertEqual("sk-synthetic", resolve_api_key("", url))
                    _validate_local_llm_base_url(url)
        for url in ("https://api.openai.com.evil.invalid/v1", "http://api.openai.com/v1"):
            with self.subTest(refused=url):
                self.assertFalse(is_official_openai_endpoint(url))
                with self.assertRaises(LLMConfigError):
                    _validate_local_llm_base_url(url)

    def test_setup_test_and_models_resolve_blank_official_openai_key(self):
        import asyncio
        from types import SimpleNamespace
        from routers import system as system_routes

        seen = []

        def factory(**kwargs):
            seen.append(kwargs.get("api_key"))
            return SimpleNamespace(
                models=SimpleNamespace(list=lambda: SimpleNamespace(data=[])),
                chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))]))))

        with patch("llm_provider.OpenAI", factory), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-synthetic"}, clear=False):
            result = system_routes._run_llm_test(
                {"base_url": "https://api.openai.com/v1", "api_key": "", "model_name": "m"})
            asyncio.run(system_routes.llm_models(system_routes.LlmModelsRequest(
                base_url="https://api.openai.com/v1", api_key="", model_name="m")))
        self.assertTrue(result["ok"], result)
        self.assertEqual(["sk-synthetic", "sk-synthetic"], seen,
                         "Setup Test and Models must resolve keys like generation does")

    def test_explicit_authorization_wins_over_unused_keys_in_both_spellings(self):
        from types import SimpleNamespace
        for header_name in ("authorization", "AUTHORIZATION"):
            with self.subTest(header_name=header_name):
                seen, clients = [], []

                def handler(request):
                    seen.append([(name, value) for name, value in request.headers.items()
                                 if name.lower() == "authorization"])
                    return self._completion()

                profile = {"base_url": "https://hosted.invalid/v1",
                           "api_key": "sk-unused-fixture",
                           "provider_headers": {header_name: "Token configured-fixture"}}
                with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env-fixture"}, clear=False), \
                        patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
                    client = make_llm_client(profile, timeout=5)
                    client.chat.completions.create(
                        model="m", messages=[{"role": "user", "content": "hi"}])
                self.assertEqual("local", clients[0].api_key)
                self.assertEqual(["Token configured-fixture"], [value for _, value in seen[0]],
                                 "only the configured header may be sent, never a derived bearer")
                self.assertEqual("local", resolve_client_api_key(profile),
                                 "an unused key must not enter receipt/benchmark identity")
                self.assertNotIn("sk-unused-fixture", str(seen))
                self.assertNotIn("sk-env-fixture", str(seen))

    def test_resolved_opaque_env_value_is_sent_verbatim_not_reparsed(self):
        with patch.dict("os.environ",
                        {"AA2_OPAQUE_FIXTURE": "env:opaque-secret-fixture"}, clear=True):
            profile = {"base_url": "https://hosted.invalid/v1",
                       "provider_headers": {"Authorization": "env:AA2_OPAQUE_FIXTURE"}}
            effective = llm_provider.get_effective_authorization(profile)
            self.assertEqual("env:opaque-secret-fixture", effective)
            self.assertEqual("env:opaque-secret-fixture",
                llm_provider.resolve_authorization_value(effective),
                "a resolved value starting with env: must not be looked up again")
            self.assertEqual("env:opaque-secret-fixture",
                llm_provider.resolve_probe_api_key(
                    {"base_url": "https://hosted.invalid/v1",
                     "api_key": "env:AA2_OPAQUE_FIXTURE"}))
            self.assertEqual("env:opaque-secret-fixture",
                llm_provider.get_provider_headers(profile)["Authorization"])

    def test_status_route_resolves_unset_env_key_to_none(self):
        import asyncio
        from routers import system as routes
        profile = {'base_url': 'https://hosted.invalid/v1', 'model_name': 'm',
                   'api_key': 'env:AA2_UNSET_FIXTURE'}
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(routes, 'load_app_config',
                             return_value={'llm_mode': 'remote', 'llm_remote': profile}), \
                patch.object(routes, 'get_current_status',
                             return_value={'available': True}) as status:
            asyncio.run(routes.lmstudio_status())
        self.assertIsNone(status.call_args.kwargs['api_key'],
                          'an unset env reference must not be sent as a raw bearer')
        self.assertIsNone(status.call_args.kwargs['authorization'])

    def test_unset_env_reference_resolves_to_authoritative_none(self):
        with patch.dict("os.environ", {"AA2_SET_FIXTURE": "sk-resolved-fixture"}, clear=True):
            self.assertIsNone(llm_provider.resolve_authorization_value("env:AA2_UNSET_FIXTURE"))
            self.assertIsNone(llm_provider.resolve_authorization_value("${AA2_UNSET_FIXTURE}"))
            self.assertEqual("sk-resolved-fixture",
                llm_provider.resolve_authorization_value("env:AA2_SET_FIXTURE"))
            self.assertEqual("Bearer bare-legacy-fixture",
                llm_provider.resolve_authorization_value("Bearer bare-legacy-fixture"))
            unset_header = {"base_url": "https://hosted.invalid/v1",
                            "provider_headers": {"Authorization": "env:AA2_UNSET_FIXTURE"}}
            self.assertIsNone(llm_provider.get_explicit_authorization(unset_header),
                              "a raw env: string must never become an Authorization value")
            self.assertEqual({"Authorization": "sk-resolved-fixture"},
                llm_provider.get_provider_headers(
                    {"provider_headers": {"authorization": "env:AA2_SET_FIXTURE"}}))
            self.assertIsNone(llm_provider.resolve_probe_api_key(
                {"base_url": "https://hosted.invalid/v1",
                 "api_key": "env:AA2_UNSET_FIXTURE"}))

    def test_manual_profile_has_no_wire_credential(self):
        profile = {'transport': 'manual', 'base_url': 'https://hosted.invalid/v1',
                   'api_key': 'sk-retained-fixture',
                   'provider_headers': {'Authorization': 'Token retained-fixture'},
                   'model_name': 'fixture'}
        self.assertIsNone(llm_provider.get_effective_authorization(profile),
                          'Manual sends no HTTP, so retained URL and headers are inert')
        self.assertEqual('manual', resolve_client_api_key(profile))

    def test_authorization_case_variants_share_one_canonical_header(self):
        seen, clients = [], []

        def handler(request):
            seen.append([(name, value) for name, value in request.headers.items()
                         if name.lower() == "authorization"])
            return self._completion()

        profile = {"base_url": "https://hosted.invalid/v1",
                   "api_key": "sk-unused-fixture",
                   "provider_headers": {"authorization": "Token first-fixture",
                                        "AUTHORIZATION": "Token first-fixture"}}
        with patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
            client = make_llm_client(profile, timeout=5)
            self.assertEqual({"Authorization": "Token first-fixture"},
                             llm_provider.get_provider_headers(profile))
            self.assertEqual("Token first-fixture", llm_provider.get_explicit_authorization(profile))
            self.assertEqual("local", resolve_client_api_key(profile),
                             "an explicit header must keep the unused key out of identity")
            client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(1, len(seen[0]),
                         "duplicate spellings must produce exactly one sent header")
        self.assertEqual("authorization", seen[0][0][0].lower())
        self.assertEqual("Token first-fixture", seen[0][0][1])
        self.assertNotIn("sk-unused-fixture", str(seen))

        profile["provider_headers"]["AUTHORIZATION"] = "Token other-fixture"
        with self.assertRaises(ValueError):
            llm_provider.get_explicit_authorization(profile)
        with self.assertRaises(ValueError):
            llm_provider.get_provider_headers(profile)

    def test_deterministic_4xx_terminal_transient_retryable(self):
        from types import SimpleNamespace
        terminal = (400, 405, 410, 413, 422)
        transient = (408, 409, 429, 503)
        for status in terminal + transient:
            with self.subTest(status=status):
                calls, clients = [], []

                def handler(request, status=status):
                    calls.append(request)
                    return httpx2.Response(status, headers={"x-should-retry": "true"}, json={
                        "error": {"message": "fixture", "type": "invalid_request_error"}})

                with patch("llm_provider.OpenAI", self._sdk_factory(handler, clients)):
                    client = make_llm_client({"base_url": "https://hosted.invalid/v1",
                                              "api_key": "fixture"}, timeout=5)
                    with self.assertRaises(Exception):
                        client.chat.completions.create(
                            model="m", messages=[{"role": "user", "content": "hi"}])
                if status in terminal:
                    self.assertEqual(1, len(calls), "a deterministic 4xx must not be retried")
                else:
                    self.assertGreater(len(calls), 1, "a transient status keeps SDK recovery")
                details = classify_llm_error(SimpleNamespace(
                    status_code=status, body={"error": {"code": "bad_request"}}))
                self.assertEqual(details["retryable"], status in transient)

    def test_setup_test_config_failure_has_no_outbound_request_and_redacts(self):
        import asyncio
        from config_settings import LLMConfig
        from routers import system as system_routes

        calls = []

        def handler(request):
            calls.append(request)
            return self._completion()

        with patch("llm_provider.OpenAI", self._sdk_factory(handler, [])), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-synthetic"}, clear=False):
            direct = system_routes._run_llm_test(
                {"base_url": "https://api.deepseek.com/v1", "api_key": "", "model_name": "m"})
            with patch.object(system_routes, "claim_gpu_task", return_value="claim-fixture") as claim, \
                    patch.object(system_routes, "release_gpu_task_claim") as release:
                routed = asyncio.run(system_routes.llm_test(
                    LLMConfig(base_url="https://api.deepseek.com/v1", api_key="", model_name="m")))
        for result in (direct, routed):
            self.assertFalse(result["ok"])
            self.assertEqual("config", result["step"])
            self.assertNotIn("log_file", result, "no request was sent, so no log is needed")
            self.assertNotIn("sk-synthetic", result["error"])
        self.assertEqual([], calls, "a config error must make zero outbound requests")
        claim.assert_called_once_with("llm_test")
        release.assert_called_once_with("llm_test", "claim-fixture")


if __name__ == "__main__":
    unittest.main()
