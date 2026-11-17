"""The remote-cost warning is answered by the active LLM profile (#2007).

WHAT THIS COST. GET /api/config's `is_remote` flag drives the SPA's
`confirmIfRemote()` prompt before every long run. It was computed from
`config["llm"]["base_url"]` - the MIRROR that `get_active_llm_config`
documents as "not a source" - while the run itself resolves its endpoint
through that resolver. With llm_mode="local", a mirror left pointing at
localhost by a benchmark caching its concurrency, a hand edit or a script,
and an active `llm_local` pointing at a billed hosted endpoint, the prompt was
skipped and the run went to the billed endpoint anyway.

These tests drive the real endpoint the browser reads, not the helper, so a
flag that is computed and then overwritten cannot pass them.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import lmstudio_settings
from routers import system, voicelab

HOSTED = "https://api.example.com/v1"
LOOPBACK = "http://localhost:11434/v1"


def make_profile(base_url, model_name="fixture-model", api_key="fixture-secret"):
    return {"base_url": base_url, "model_name": model_name, "api_key": api_key}


class RemoteWarningConfigTests(unittest.TestCase):

    def read_config(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps(document), encoding="utf-8")
            application = FastAPI()
            application.include_router(system.router)
            with patch.object(system, "CONFIG_PATH", str(path)), \
                    patch.object(system, "DATA_DIR", tmp), \
                    patch.object(system, "project_manager", Mock()), \
                    TestClient(application) as client:
                response = client.get("/api/config")
        self.assertEqual(200, response.status_code)
        return response.json()

    def test_a_stale_local_mirror_cannot_hide_a_hosted_active_profile(self):
        """The #2007 failure: the prompt the user never saw.

        A mirror still saying localhost must not turn `is_remote` off while the
        profile the run will actually use is a billed hosted endpoint.
        """
        returned = self.read_config({
            "llm_mode": "local",
            "llm_local": make_profile(HOSTED),
            "llm": make_profile(LOOPBACK),
        })
        self.assertEqual(HOSTED, returned["llm_local"]["base_url"],
                         "the fixture must keep the hosted active profile")
        self.assertTrue(returned["is_remote"],
                        "a run from this config dials a hosted endpoint, so the "
                        "cost prompt must be shown")

    def test_a_stale_hosted_mirror_cannot_invent_a_remote_run(self):
        """The reverse drift: the mirror says hosted, the run is local."""
        returned = self.read_config({
            "llm_mode": "local",
            "llm_local": make_profile(LOOPBACK),
            "llm": make_profile(HOSTED),
        })
        self.assertFalse(returned["is_remote"])

    def test_remote_mode_reports_remote(self):
        returned = self.read_config({
            "llm_mode": "remote",
            "llm_remote": make_profile(HOSTED),
            "llm": make_profile(LOOPBACK),
        })
        self.assertTrue(returned["is_remote"])

    def test_the_resolver_sees_the_real_config_and_redaction_still_applies(self):
        """Order matters: classify the loaded config, redact what is returned.

        Computing the flag after `_redact_config_secrets` would classify a
        masked copy; returning the unredacted copy would put the credential on
        the wire. Both are asserted against the same response.
        """
        returned = self.read_config({
            "llm_mode": "local",
            "llm_local": make_profile(HOSTED),
            "llm": make_profile(LOOPBACK),
        })
        self.assertTrue(returned["is_remote"])
        for section in ("llm_local", "llm"):
            with self.subTest(section=section):
                self.assertEqual("[REDACTED]", returned[section]["api_key"])
                self.assertTrue(returned[section]["api_key_configured"])
                self.assertEqual(HOSTED if section == "llm_local" else LOOPBACK,
                                 returned[section]["base_url"])


class ActiveProfileClassificationTests(unittest.TestCase):

    def test_the_mirror_never_decides_the_classification(self):
        cases = (
            ("local", HOSTED, True),
            ("local", LOOPBACK, False),
            ("remote", LOOPBACK, True),
        )
        for mode, active_url, expected in cases:
            stale_url = LOOPBACK if active_url == HOSTED else HOSTED
            config = {"llm_mode": mode,
                      f"llm_{mode}": make_profile(active_url),
                      "llm": make_profile(stale_url)}
            with self.subTest(mode=mode, active_url=active_url):
                self.assertIs(expected, lmstudio_settings.is_active_llm_remote(config))

    def test_garbage_config_is_local_rather_than_an_exception(self):
        for bad in (None, [], "config", 7, {}):
            with self.subTest(config=bad):
                self.assertIs(False, lmstudio_settings.is_active_llm_remote(bad))


class SupportSummaryTests(unittest.TestCase):

    def test_the_voicelab_summary_classifies_the_active_profile(self):
        """The diagnostics bundle reports the same question as the Setup page,
        so it must not repeat the mirror's stale answer."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({
                "llm_mode": "local",
                "llm_local": make_profile(HOSTED),
                "llm": make_profile(LOOPBACK),
            }), encoding="utf-8")
            with patch.object(voicelab, "CONFIG_PATH", str(path)):
                summary = voicelab._safe_config_summary()
        self.assertTrue(summary["available"])
        self.assertEqual("local", summary["llm_mode"])
        self.assertTrue(summary["is_remote_llm"])


if __name__ == "__main__":
    unittest.main()
