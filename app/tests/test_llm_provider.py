import json
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from config_settings import LLMConfig
from llm_provider import ConfiguredOpenAI, make_llm_client, merge_provider_extra_body


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


if __name__ == "__main__":
    unittest.main()
