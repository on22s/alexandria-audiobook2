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
             "parallel": 1, "optimized": True}

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
        """The probe returns None for anything that is not llama.cpp, and that
        path must be untouched - skipping it would be the opposite bug."""
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status",
                               return_value=None), \
             mock.patch.object(lmstudio_settings, "apply_lmstudio_settings",
                               return_value=(True, "loaded with ideal settings")) as apply_, \
             mock.patch.object(lmstudio_settings, "get_current_status",
                               return_value={"available": True, "loaded": False,
                                             "context_length": 8192,
                                             "parallel": 1, "optimized": False}):
            message = lmstudio_settings.ensure_ideal_settings(
                "local", "http://127.0.0.1:1234/v1", "qwen3-14b", None)[2]
        apply_.assert_called_once()
        self.assertIn("LM Studio", message)

    def test_a_remote_endpoint_is_unaffected_by_the_local_probe(self):
        """Remote returns before the local branch; the probe must not run at
        all, since it would be a network call to the wrong host."""
        with mock.patch.object(lmstudio_settings, "get_llama_cpp_status") as probe, \
             mock.patch.object(lmstudio_settings, "get_remote_lmstudio_status",
                               return_value={"available": False, "loaded": False,
                                             "context_length": None,
                                             "parallel": None,
                                             "optimized": False}):
            lmstudio_settings.ensure_ideal_settings(
                "remote", "http://10.0.0.5:1234/v1", "qwen3-14b", None)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
