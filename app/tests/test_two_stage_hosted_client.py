"""two_stage_attribution can score a hosted API, and still guards the local card.

The harness hardcoded api_key "local" and always demanded a free local GPU, so it
could not produce a cloud reference on WP2021 even though lora_serving_eval can.
The hosted path mirrors lora_serving_eval: key from an environment variable, the
provider body merged through the product's ConfiguredOpenAI, no GPU guard.
"""
import unittest
from unittest.mock import patch

from experiments import two_stage_attribution as ts


class HostedClientTests(unittest.TestCase):

    def test_a_local_endpoint_still_takes_the_gpu_guard(self):
        with patch.object(ts, "require_free_gpu", side_effect=RuntimeError("card busy")):
            with self.assertRaises(RuntimeError):
                ts.build_client("http://127.0.0.1:8090/v1")

    def test_a_hosted_endpoint_skips_the_guard_and_uses_the_key(self):
        with patch.object(ts, "require_free_gpu", side_effect=RuntimeError("card busy")):
            client = ts.build_client("https://api.example.com/v1", api_key="sk-test", hosted=True)
        self.assertEqual("sk-test", client.api_key)

    def test_the_provider_body_is_merged_into_each_request(self):
        seen = {}

        class FakeCompletions:
            def create(self, **kw):
                seen.update(kw)
                return "ok"

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, **kw):
                self.api_key = kw.get("api_key")
                self.chat = FakeChat()

        with patch.object(ts, "require_free_gpu"), patch("openai.OpenAI", FakeOpenAI):
            client = ts.build_client("https://api.example.com/v1", api_key="k", hosted=True,
                                     extra_body={"thinking": {"type": "disabled"}})
            client.chat.completions.create(model="m", messages=[],
                                           extra_body={"reasoning_effort": "none"})
        self.assertEqual({"type": "disabled"}, seen["extra_body"]["thinking"])
        self.assertEqual("none", seen["extra_body"]["reasoning_effort"])


if __name__ == "__main__":
    unittest.main()
