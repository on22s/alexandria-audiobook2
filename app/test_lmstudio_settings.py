import unittest
from unittest.mock import patch

import lmstudio_settings


class DynamicLmStudioSettingsTests(unittest.TestCase):
    MODEL = "gemma-4-e4b-uncensored-hauhaucs-aggressive"

    def test_verified_profile_selected_with_headroom(self):
        settings = lmstudio_settings.get_safe_local_settings(
            self.MODEL, True, (17 * 1024 ** 3, 10 * 1024 ** 3))
        self.assertEqual((32768, 2),
                         (settings["context_length"], settings["parallel"]))

    def test_external_vram_pressure_uses_fallback(self):
        settings = lmstudio_settings.get_safe_local_settings(
            self.MODEL, True, (17 * 1024 ** 3, 16 * 1024 ** 3))
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_unknown_model_uses_fallback(self):
        settings = lmstudio_settings.get_safe_local_settings(
            "another-model", True, (17 * 1024 ** 3, 1))
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_missing_gpu_metrics_use_fallback(self):
        with patch.object(lmstudio_settings, "get_local_vram_bytes", return_value=None):
            settings = lmstudio_settings.get_safe_local_settings(self.MODEL, True)
        self.assertEqual((8192, 1),
                         (settings["context_length"], settings["parallel"]))

    def test_planned_local_settings_use_verified_target_without_mutating(self):
        status = {"context_length": None, "parallel": None,
                  "ideal_context_length": 32768, "ideal_parallel": 2,
                  "settings_reason": "verified profile"}
        with patch.object(lmstudio_settings, "get_current_status",
                          return_value=status) as current:
            planned = lmstudio_settings.get_planned_ideal_settings(
                "local", "http://localhost:1234/v1", self.MODEL)

        self.assertEqual((32768, 2),
                         (planned["context_length"], planned["parallel"]))
        current.assert_called_once()

    def test_planned_remote_settings_use_remote_target_without_status_call(self):
        with patch.object(lmstudio_settings, "get_current_status") as current:
            planned = lmstudio_settings.get_planned_ideal_settings(
                "remote", "http://remote:1234/v1", self.MODEL, "tnr-0")

        self.assertEqual((98304, 2),
                         (planned["context_length"], planned["parallel"]))
        current.assert_not_called()


if __name__ == "__main__":
    unittest.main()
