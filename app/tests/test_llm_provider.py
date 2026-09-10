import json
import time
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from config_settings import LLMConfig
from llm_provider import (ConfiguredOpenAI, get_profile_timeout, make_llm_client,
                          merge_provider_extra_body, classify_llm_error,
                          get_retry_delay)


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

    def _configured_client_with_capture(self, provider_extra_body):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": "model", "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            })

        raw_client = OpenAI(
            base_url="http://provider.test/v1", api_key="key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
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


if __name__ == "__main__":
    unittest.main()
