import unittest

from config_settings import AppConfig, LLMConfig, TTSConfig
from routers.system import _redact_config_secrets, _restore_redacted_secrets


class ConfigSecretRedactionTests(unittest.TestCase):
    def test_get_shape_masks_credentials_without_mutating_input(self):
        config = {
            "llm": {"api_key": "llm-secret", "model_name": "model"},
            "llm_local": {"api_key": "local-secret"},
            "tts": {"api_key": "tts-secret"},
        }
        safe = _redact_config_secrets(config)

        self.assertEqual("llm-secret", config["llm"]["api_key"])
        self.assertEqual("[REDACTED]", safe["llm"]["api_key"])
        self.assertTrue(safe["llm"]["api_key_configured"])
        self.assertEqual("[REDACTED]", safe["llm_local"]["api_key"])
        self.assertEqual("[REDACTED]", safe["tts"]["api_key"])

    def test_sentinel_round_trip_preserves_saved_secret(self):
        profile = LLMConfig(base_url="http://localhost:1234/v1",
                            api_key="[REDACTED]", model_name="model")
        config = AppConfig(
            llm=profile,
            llm_local=profile,
            tts=TTSConfig(),
        )
        restored = _restore_redacted_secrets(config, {
            "llm": {"api_key": "saved-llm"},
            "llm_local": {"api_key": "saved-local"},
        })

        self.assertEqual("saved-llm", restored.llm.api_key)
        self.assertEqual("saved-local", restored.llm_local.api_key)


if __name__ == "__main__":
    unittest.main()
