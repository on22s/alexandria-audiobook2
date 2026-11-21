"""Do not try to configure a server you do not own.

Every unseen-book run on 2026-08-19 logged:

    LM Studio: WARNING - could not apply ideal settings (Error: LM Studio
    daemon is not running ...). The model may be running with a higher
    'parallel'/context-length configuration, which ... increases the risk of
    an out-of-memory crash.

against http://127.0.0.1:8090/v1, which is llama.cpp. The warning is both
alarming and inapplicable: llama.cpp fixes context and slot count at launch,
so there is nothing to apply and no risk to warn about. `get_llama_cpp_status`
already answered "whose server is this?" and the local branch never asked it.
"""
import os
import sys
import unittest
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import lmstudio_settings  # noqa: E402


class IdealSettingsEndpointTest(unittest.TestCase):
    LLAMA = {"available": True, "loaded": True, "context_length": 32768,
             "parallel": 1, "optimized": None, "runtime": "llama.cpp"}

    def test_a_llama_cpp_endpoint_is_left_alone(self):
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=dict(self.LLAMA)) as probe, \
             mock.patch.object(lmstudio_settings, "apply_lmstudio_settings") as apply_:
            is_remote, status, message = lmstudio_settings.ensure_ideal_settings(
                "local", "http://127.0.0.1:8090/v1", "qwen3-14b", None)
        probe.assert_called_once()
        apply_.assert_not_called()
        self.assertFalse(is_remote)
        self.assertEqual(32768, status["context_length"])

    def test_the_message_does_not_warn_about_a_risk_that_cannot_occur(self):
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=dict(self.LLAMA)), \
             mock.patch.object(lmstudio_settings, "apply_lmstudio_settings"):
            message = lmstudio_settings.ensure_ideal_settings(
                "local", "http://127.0.0.1:8090/v1", "qwen3-14b", None)[2]
        lowered = message.lower()
        self.assertNotIn("warning", lowered)
        self.assertNotIn("out-of-memory", lowered)
        self.assertNotIn("restart lm studio", lowered)
        self.assertIn("llama.cpp", lowered)
        self.assertIn("32768", message)

    def test_a_real_lm_studio_endpoint_still_gets_configured(self):
        """A positively verified LM Studio endpoint retains managed reloads."""
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=None), \
             mock.patch.object(lmstudio_settings, "apply_lmstudio_settings",
                               return_value=(True, "loaded with ideal settings")) as apply_, \
             mock.patch.object(lmstudio_settings, "get_current_status",
                               return_value={"management_verified": True, "available": True, "loaded": False,
                                             "context_length": 8192,
                                             "parallel": 1, "optimized": False}):
            message = lmstudio_settings.ensure_ideal_settings(
                "local", "http://127.0.0.1:1234/v1", "qwen3-14b", None)[2]
        apply_.assert_called_once()
        self.assertIn("LM Studio", message)

    def test_manual_preflight_declares_limits_without_any_runtime_probe(self):
        profile = {"transport": "manual", "base_url": "https://hosted.invalid/v1",
                   "model_name": "m", "api_key": "sk-retained-fixture",
                   "context_length": 16384,
                   "provider_headers": {"Authorization": "Token retained-fixture"}}
        def forbidden(*args, **kwargs):
            raise AssertionError("Manual transport must not make any HTTP probe")
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               side_effect=forbidden), \
             mock.patch.object(lmstudio_settings, "get_lmstudio_endpoint_status",
                               side_effect=forbidden), \
             mock.patch.object(lmstudio_settings, "get_runtime_status_cached",
                               side_effect=forbidden):
            status = lmstudio_settings.get_current_status(
                "remote", profile["base_url"], "m", use_cache=True,
                api_key="sk-retained-fixture", authorization="Token retained-fixture",
                llm_config=profile)
            is_remote, healed, message = lmstudio_settings.ensure_ideal_settings(
                "remote", profile["base_url"], "m", "fixture-host",
                api_key="sk-retained-fixture", authorization="Token retained-fixture",
                llm_config=profile)
        self.assertFalse(is_remote)
        self.assertEqual(16384, status["context_length"])
        self.assertEqual(1, status["parallel"])
        self.assertEqual(16384, healed["context_length"])
        self.assertEqual(1, healed["parallel"])
        self.assertIn("Manual", message)

    def test_explicit_non_bearer_authorization_reaches_the_status_probe(self):
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=None) as probe, \
             mock.patch.object(lmstudio_settings, "get_current_status",
                               return_value=dict(self.LLAMA)) as status:
            lmstudio_settings.ensure_ideal_settings(
                "remote", "https://hosted.invalid/v1", "qwen3-14b", "fixture-host",
                api_key="sk-unused-fixture", authorization="Token configured-fixture")
        self.assertEqual("Token configured-fixture", status.call_args.kwargs["authorization"])
        self.assertEqual("sk-unused-fixture", status.call_args.kwargs["api_key"])

    def test_a_remote_endpoint_is_unaffected_by_the_local_probe(self):
        """Remote endpoints are probed too, so remote llama.cpp is not sent
        LM Studio SSH management commands."""
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=None) as probe, \
             mock.patch.object(lmstudio_settings, "get_remote_lmstudio_status",
                               return_value={"available": False, "loaded": False,
                                             "context_length": None,
                                             "parallel": None,
                                             "optimized": False}):
            lmstudio_settings.ensure_ideal_settings(
                "remote", "http://10.0.0.5:1234/v1", "qwen3-14b", None)
        probe.assert_called_once()


class ProfileBackedPreflightWiringTests(unittest.TestCase):
    def test_every_production_preflight_forwards_its_bound_profile_credential(self):
        import inspect
        for name, profile in (("generate_script", "llm_config"),
                              ("three_pass_generate", "llm"),
                              ("generate_personas", "llm_cfg"),
                              ("find_nicknames", "llm"),
                              ("integration_runner", "llm"),
                              ("routers.script", "llm"),
                              ("core", "llm_cfg"),
                              ("review_script", "llm_config"),
                              ("llm_bench", "credentials"),
                              ("routers.voices", "llm_cfg"),
                              ("routers.system", "cfg")):
            with self.subTest(module=name):
                module = __import__(name, fromlist=["*"])
                source = inspect.getsource(module)
                self.assertIn(f"authorization=get_effective_authorization({profile})", source)
                self.assertIn(f"llm_config={profile}", source,
                              "the status probe must see the bound profile so Manual "
                              "transport can bypass every HTTP probe")


if __name__ == "__main__":
    unittest.main()
