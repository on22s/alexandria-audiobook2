import copy
import json
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
            "llm": {"base_url": profile.base_url, "api_key": "saved-llm"},
            "llm_local": {"base_url": profile.base_url, "api_key": "saved-local"},
        })

        self.assertEqual("saved-llm", restored.llm.api_key)
        self.assertEqual("saved-local", restored.llm_local.api_key)




class ProviderHeaderRedactionTests(unittest.TestCase):
    """#2006: provider_headers reach the provider as the OpenAI client's default
    headers, so `Authorization: Bearer ...` and `x-api-key` are credentials that
    must not come back from GET /api/config."""

    def headers_config(self):
        return {
            "llm": {"api_key": "llm-key",
                    "provider_headers": {"Authorization": "Bearer llm-secret",
                                         "x-api-key": "llm-api-secret"}},
            "llm_local": {"api_key": "local-key",
                          "provider_headers": {"X-Acme-Token": "local-secret"}},
            "llm_remote": {"api_key": "remote-key",
                           "provider_headers": {"Authorization": "Bearer remote-secret"}},
            "tts": {"api_key": "tts-key"},
        }

    def test_every_profile_header_value_is_masked_and_every_name_is_kept(self):
        config = self.headers_config()
        safe = _redact_config_secrets(config)

        for section in ("llm", "llm_local", "llm_remote"):
            self.assertEqual(set(config[section]["provider_headers"]),
                             set(safe[section]["provider_headers"]))
            self.assertEqual({"[REDACTED]"},
                             set(safe[section]["provider_headers"].values()))
        serialized = json.dumps(safe)
        for secret in ("Bearer llm-secret", "llm-api-secret", "local-secret",
                       "Bearer remote-secret"):
            self.assertNotIn(secret, serialized)

    def test_redaction_leaves_the_input_config_unchanged(self):
        config = self.headers_config()
        original = copy.deepcopy(config)
        _redact_config_secrets(config)
        self.assertEqual(original, config)

    def test_echoed_sentinel_keeps_changed_value_is_replaced_and_removed_stays_removed(self):
        saved = {"llm_local": {"base_url": "http://127.0.0.1:1234/v1", "api_key": "local-key",
                               "provider_headers": {"Authorization": "Bearer stored",
                                                    "X-Remove-Me": "stored-2"}}}
        incoming = LLMConfig(base_url="http://127.0.0.1:1234/v1", api_key="local-key",
                             model_name="model",
                             provider_headers={"Authorization": "[REDACTED]",
                                               "X-Changed": "typed-new"})
        config = AppConfig(llm=incoming, llm_local=incoming, tts=TTSConfig())
        restored = _restore_redacted_secrets(config, saved)

        self.assertEqual({"Authorization": "Bearer stored", "X-Changed": "typed-new"},
                         restored.llm_local.provider_headers)
        # Rule 17: the config handed in is unchanged.
        self.assertEqual({"Authorization": "[REDACTED]", "X-Changed": "typed-new"},
                         config.llm_local.provider_headers)

    def test_sentinel_with_no_stored_header_is_dropped_not_persisted(self):
        saved = {"llm": {"base_url": "http://127.0.0.1:1234/v1", "api_key": "k",
                          "provider_headers": {"Authorization": "Bearer stored"}}}
        incoming = LLMConfig(base_url="http://127.0.0.1:1234/v1", api_key="k",
                             model_name="model",
                             provider_headers={"Authorization": "[REDACTED]",
                                               "X-Unknown": "[REDACTED]"})
        config = AppConfig(llm=incoming, tts=TTSConfig())
        restored = _restore_redacted_secrets(config, saved)

        self.assertEqual({"Authorization": "Bearer stored"}, restored.llm.provider_headers)
        self.assertNotIn("[REDACTED]", json.dumps(restored.llm.provider_headers))


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
        # An unmatched endpoint must never inherit the active profile's key.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            _resolve_redacted_api_key("[REDACTED]", "https://other/v1", existing)
        # a real key typed by the user passes through untouched
        self.assertEqual("sk-typed", _resolve_redacted_api_key("sk-typed", "https://api.example.com/v1", existing))


class RedactedHeaderResolutionTests(unittest.TestCase):
    """The Test / Load-models buttons send the header editor as-is, which after
    the GET redaction is one sentinel per stored header; they must still send
    the real credential, and never the literal placeholder."""

    EXISTING = {"llm_local": {"base_url": "http://127.0.0.1:8090/v1",
                              "api_key": "local",
                              "provider_headers": {"Authorization": "Bearer stored"}}}

    def test_sentinels_resolve_from_the_profile_at_the_same_endpoint(self):
        from routers.system import _resolve_redacted_provider_headers
        resolved = _resolve_redacted_provider_headers(
            {"Authorization": "[REDACTED]", "X-Changed": "typed"},
            "http://127.0.0.1:8090/v1", self.EXISTING)
        self.assertEqual({"Authorization": "Bearer stored", "X-Changed": "typed"}, resolved)

    def test_unmatched_endpoint_and_unknown_headers_never_send_the_sentinel(self):
        from routers.system import _resolve_redacted_provider_headers
        self.assertEqual({}, _resolve_redacted_provider_headers(
            {"Authorization": "[REDACTED]"}, "https://other.invalid/v1", self.EXISTING))
        self.assertEqual({"Authorization": "Bearer stored"},
                         _resolve_redacted_provider_headers(
                             {"Authorization": "[REDACTED]", "X-Unknown": "[REDACTED]"},
                             "http://127.0.0.1:8090/v1", self.EXISTING))


class RedactionKeepsNonJsonObjects(unittest.TestCase):
    def test_pydantic_values_survive_redaction(self):
        """A loaded config carries model objects; the copy must not need JSON."""
        from config_settings import ThreePassModelProfile
        profile = ThreePassModelProfile()
        config = {"llm": {"api_key": "k"}, "three_pass": {"profiles": {"x": profile}}}
        safe = _redact_config_secrets(config)
        self.assertIs(type(safe["three_pass"]["profiles"]["x"]), ThreePassModelProfile)
        self.assertEqual("[REDACTED]", safe["llm"]["api_key"])


if __name__ == "__main__":
    unittest.main()
