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


if __name__ == "__main__":
    unittest.main()
