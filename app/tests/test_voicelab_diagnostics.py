"""Phase 5 — sanitized diagnostics redaction and bounding tests.

Exercises diagnostics.py directly (pure module, no server) across the secret
forms the plan calls out: API keys, bearer/basic auth, URL credentials, nested
secrets, home paths, oversized values, malformed records, and Unicode.
"""

import json
import unittest

import diagnostics


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_fully_redacted_at_any_depth(self):
        obj = {
            "api_key": "sk-secret",
            "llm": {"API_KEY": "abc", "model_name": "gemma", "base_url": "http://x"},
            "headers": {"Authorization": "Bearer xyz", "X-Auth-Token": "t"},
            "client_secret": "cs", "session_cookie": "c", "passphrase": "p",
            "list": [{"password": "pw"}, {"safe": "ok"}],
        }
        out = diagnostics.redact(obj)
        self.assertEqual(diagnostics.REDACTED, out["api_key"])
        self.assertEqual(diagnostics.REDACTED, out["llm"]["API_KEY"])
        self.assertEqual("gemma", out["llm"]["model_name"])  # non-sensitive kept
        self.assertEqual(diagnostics.REDACTED, out["headers"]["Authorization"])
        self.assertEqual(diagnostics.REDACTED, out["headers"]["X-Auth-Token"])
        self.assertEqual(diagnostics.REDACTED, out["client_secret"])
        self.assertEqual(diagnostics.REDACTED, out["session_cookie"])
        self.assertEqual(diagnostics.REDACTED, out["passphrase"])
        self.assertEqual(diagnostics.REDACTED, out["list"][0]["password"])
        self.assertEqual("ok", out["list"][1]["safe"])

    def test_url_credentials_in_values_are_stripped(self):
        out = diagnostics.redact({"endpoint": "https://user:hunter2@example.com/v1"})
        self.assertNotIn("hunter2", out["endpoint"])
        self.assertIn("[REDACTED]@example.com", out["endpoint"])

    def test_inline_credentials_in_free_text_are_scrubbed(self):
        line = "connecting with token=abc123 and password: p@ss to server"
        out = diagnostics.redact_text(line)
        self.assertNotIn("abc123", out)
        self.assertNotIn("p@ss", out)
        self.assertIn("[REDACTED]", out)

    def test_bare_secret_tokens_are_scrubbed_by_shape(self):
        # A credential stored as a plain value under an innocent key must still be
        # scrubbed by its recognizable format.
        secrets = {
            "note": "key is sk-abcdEFGH1234ijklMNOP5678zzzz",
            "detail": "pushed with ghp_16CharsMinimumAbc123456789",
            "jwt": "eyJhbGciOi.eyJzdWIiOiJ4.SflKxwRJSMeKKF2QT4",
            "slack": "hook xoxb-1234567890-abcdefghij",
        }
        out = diagnostics.redact(secrets)
        for value in out.values():
            self.assertIn("[REDACTED]", value)
        self.assertNotIn("sk-abcdEFGH", out["note"])
        self.assertNotIn("ghp_16Chars", out["detail"])
        self.assertNotIn("eyJzdWIiOiJ4", out["jwt"])

    def test_bearer_token_is_scrubbed_without_leaking_the_tail(self):
        # Regression: the inline key=value rule used to eat only "Bearer" and
        # leave the real token exposed after it.
        token = "aVeryLongOpaqueBearerToken1234567890"
        out = diagnostics.redact_text(f"Authorization: Bearer {token}")
        self.assertNotIn(token, out)

    def test_surfaced_hashes_and_revisions_are_not_mistaken_for_secrets(self):
        # The bundle deliberately surfaces git revisions and sha256 evidence
        # hashes (pure hex); the token heuristic must never clobber them.
        git_sha = "5f5cdc6a1b2c3d4e5f60718293a4b5c6d7e8f901"          # 40 hex
        sha256 = "341cc1ffb7b3e1d623c36460dbf87afa2f4f26f946d7c9427e3176de6891aab6"  # 64 hex
        for value in (git_sha, sha256, "dfa059c1", "just some ordinary prose here"):
            self.assertEqual(value, diagnostics.redact_text(value))

    def test_home_paths_are_collapsed(self):
        out = diagnostics.redact_text("/home/alice/models/adapter and /Users/bob/x",
                                      home_dir="/home/alice")
        self.assertNotIn("alice", out)
        self.assertNotIn("bob", out)
        self.assertNotIn("/home/", out)
        self.assertNotIn("/Users/", out)

    def test_long_strings_are_truncated_with_marker(self):
        out = diagnostics.redact_text("x" * (diagnostics.MAX_STRING_CHARS + 500))
        self.assertTrue(out.endswith(diagnostics.STRING_TRUNCATION_MARKER))
        self.assertLessEqual(len(out),
                             diagnostics.MAX_STRING_CHARS + len(diagnostics.STRING_TRUNCATION_MARKER))

    def test_long_lists_are_capped_with_marker(self):
        out = diagnostics.redact(list(range(diagnostics.MAX_LIST_ITEMS + 20)))
        self.assertEqual(diagnostics.MAX_LIST_ITEMS + 1, len(out))
        self.assertEqual(diagnostics.LIST_TRUNCATION_MARKER, out[-1])

    def test_unicode_is_preserved(self):
        out = diagnostics.redact({"note": "café — 日本語 — naïve"})
        self.assertEqual("café — 日本語 — naïve", out["note"])

    def test_non_string_scalars_pass_through(self):
        out = diagnostics.redact({"n": 3, "f": 1.5, "b": True, "z": None})
        self.assertEqual({"n": 3, "f": 1.5, "b": True, "z": None}, out)


class BundleTests(unittest.TestCase):
    def test_bundle_is_versioned_and_wraps_sections(self):
        bundle = diagnostics.build_diagnostics({"runtime": {"python": "3.10"}})
        self.assertEqual(diagnostics.SCHEMA_VERSION, bundle["schema_version"])
        self.assertIn("generated_at", bundle)
        self.assertEqual("3.10", bundle["sections"]["runtime"]["python"])

    def test_bundle_redacts_secrets_in_sections(self):
        bundle = diagnostics.build_diagnostics(
            {"config": {"api_key": "sk-live", "base_url": "https://u:p@h/v1"}})
        blob = json.dumps(bundle)
        self.assertNotIn("sk-live", blob)
        self.assertNotIn("u:p@h", blob)

    def test_malformed_none_section_is_bounded_not_fatal(self):
        # A missing/unavailable section arrives as None and must not raise.
        bundle = diagnostics.build_diagnostics({"latest_run": None, "logs": []})
        self.assertIsNone(bundle["sections"]["latest_run"])
        self.assertEqual([], bundle["sections"]["logs"])

    def test_oversized_bundle_is_dropped_to_stay_within_budget(self):
        # Many keys of near-max strings — dict size is not capped by redaction,
        # so this survives bounding and still blows the total-byte budget.
        huge = {f"k{i}": "y" * 1900 for i in range(60)}
        bundle = diagnostics.build_diagnostics({"runtime": {"python": "3.10"}, "huge": huge})
        size = len(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(size, diagnostics.MAX_TOTAL_BYTES)
        # Largest section dropped; small one retained.
        self.assertIn("omitted", json.dumps(bundle["sections"]["huge"]))
        self.assertEqual("3.10", bundle["sections"]["runtime"]["python"])


if __name__ == "__main__":
    unittest.main()
