import unittest
from unittest.mock import patch

from config_settings import LLMConfig
from llm_provider import make_llm_client, merge_provider_extra_body


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


if __name__ == "__main__":
    unittest.main()
