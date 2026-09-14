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


class RedactedKeyResolutionTests(unittest.TestCase):
    """The Test / Load-models buttons send the key field as-is, which after
    the GET redaction is the sentinel; it must map back to the stored key."""

    def test_sentinel_resolves_to_the_matching_profile_key(self):
        from routers.system import _resolve_redacted_api_key
        existing = {"llm_mode": "remote",
                    "llm_local": {"base_url": "http://127.0.0.1:8090/v1", "api_key": "local"},
                    "llm_remote": {"base_url": "https://api.example.com/v1/", "api_key": "sk-real"}}
        self.assertEqual("sk-real", _resolve_redacted_api_key("[REDACTED]", "https://api.example.com/v1", existing))
        self.assertEqual("local", _resolve_redacted_api_key("[REDACTED]", "http://127.0.0.1:8090/v1", existing))
        # an unmatched URL falls back to the active profile's key, never the sentinel
        self.assertEqual("sk-real", _resolve_redacted_api_key("[REDACTED]", "https://other/v1", existing))
        # a real key typed by the user passes through untouched
        self.assertEqual("sk-typed", _resolve_redacted_api_key("sk-typed", "https://api.example.com/v1", existing))


class RedactionKeepsNonJsonObjects(unittest.TestCase):
    def test_pydantic_values_survive_redaction(self):
        """A loaded config carries model objects; the copy must not need JSON."""
        from config_settings import ThreePassModelProfile
        profile = ThreePassModelProfile()
        config = {"llm": {"api_key": "k"}, "three_pass": {"profiles": {"x": profile}}}
        safe = _redact_config_secrets(config)
        self.assertIs(type(safe["three_pass"]["profiles"]["x"]), ThreePassModelProfile)
        self.assertEqual("[REDACTED]", safe["llm"]["api_key"])
