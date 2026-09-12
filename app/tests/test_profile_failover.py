"""When the active LLM profile gives up - retries exhausted, or a content-policy
refusal - a run can switch to the other profile for the rest of the run."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import generate_script as gs
from llm_provider import FailoverClient, make_run_client
from lmstudio_settings import get_failover_llm_config


def _fake(script, seen):
    """A client whose create() pops the next item: raise if Exception, else
    return it as JSON content. Records the model each request named."""
    items = list(script)

    def create(**kwargs):
        seen.append(kwargs.get("model"))
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(item)), finish_reason="stop")], usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                             with_options=lambda **k: client)
    return client


RATE_LIMITED = type("RateLimited", (Exception,), {"status_code": 429})
BLOCKED = type("Blocked", (Exception,), {"status_code": 400})
GOOD = [{"type": "NARRATOR", "text": "He waited."}]
BASE = {"base_url": "http://127.0.0.1:1234/v1", "api_key": "k", "model_name": "local-model"}
REMOTE = {"base_url": "http://10.0.0.9:1234/v1", "api_key": "k", "model_name": "remote-model"}


class FailoverConfigTests(unittest.TestCase):
    def test_off_by_default_resolves_to_nothing(self):
        self.assertEqual({}, get_failover_llm_config({"llm_mode": "local", "llm_local": BASE, "llm_remote": REMOTE}))

    def test_on_resolves_to_the_other_profile_in_both_directions(self):
        cfg = {"llm_failover": True, "llm_mode": "local", "llm_local": BASE, "llm_remote": REMOTE}
        self.assertEqual(REMOTE, get_failover_llm_config(cfg))
        cfg["llm_mode"] = "remote"
        self.assertEqual(BASE, get_failover_llm_config(cfg))

    def test_an_unconfigured_other_profile_means_no_failover(self):
        cfg = {"llm_failover": True, "llm_mode": "local", "llm_local": BASE,
               "llm_remote": {"base_url": "", "api_key": "k", "model_name": ""}}
        self.assertEqual({}, get_failover_llm_config(cfg))

    def test_make_run_client_is_plain_unless_failover_applies(self):
        with patch("llm_provider.make_llm_client", lambda cfg, t: SimpleNamespace(cfg=cfg)):
            plain = make_run_client({"llm_mode": "local", "llm_local": BASE, "llm_remote": REMOTE}, BASE, 30)
            self.assertFalse(isinstance(plain, FailoverClient))
            fo = make_run_client({"llm_failover": True, "llm_mode": "local", "llm_local": BASE, "llm_remote": REMOTE}, BASE, 30)
            self.assertIsInstance(fo, FailoverClient)
            self.assertEqual("local-model", fo.active_model)


class FailoverClientTests(unittest.TestCase):
    def test_requests_go_to_the_primary_until_a_switch_then_the_secondary_with_its_model(self):
        seen_p, seen_s = [], []
        fo = FailoverClient(_fake([GOOD, GOOD], seen_p), "local-model", _fake([GOOD], seen_s), "remote-model")
        fo.chat.completions.create(model="local-model", messages=[])
        self.assertTrue(fo.failover({"category": "rate_limited", "status_code": 429}))
        fo.chat.completions.create(model="local-model", messages=[])
        self.assertEqual(["local-model"], seen_p)
        self.assertEqual(["remote-model"], seen_s, "the caller's model name is replaced by the secondary's")

    def test_switching_is_sticky_and_a_second_switch_reports_nothing_left(self):
        fo = FailoverClient(_fake([], []), "a", _fake([], []), "b")
        self.assertTrue(fo.failover({"category": "server_error", "status_code": 503}))
        self.assertFalse(fo.failover({"category": "server_error", "status_code": 503}))
        self.assertTrue(fo.switched)

    def test_with_options_keeps_the_pair_and_the_switch(self):
        fo = FailoverClient(_fake([], []), "a", _fake([], []), "b")
        fo.failover({"category": "timeout", "status_code": None})
        clone = fo.with_options(timeout=5)
        self.assertIsInstance(clone, FailoverClient)
        self.assertTrue(clone.switched)


class RetryLoopFailoverTests(unittest.TestCase):
    def _params(self):
        return gs.LLMGenParams(system_prompt="s", user_prompt_template="{batch}", temperature=0.0,
                               max_tokens=64, api_retry_limit=0, retry_initial_delay_seconds=0)

    def _call(self, client):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, "REPO", tmp, create=True):
            return gs.call_llm_for_entries(client, "local-model", "sys", "user", self._params(),
                                           log_name="t.log", label="T", max_retries=0)

    def test_rate_limit_exhaustion_switches_and_the_request_completes_on_the_other_profile(self):
        seen_p, seen_s = [], []
        fo = FailoverClient(_fake([RATE_LIMITED("429")], seen_p), "local-model",
                            _fake([GOOD], seen_s), "remote-model")
        self.assertEqual(GOOD, self._call(fo))
        self.assertEqual(["local-model"], seen_p)
        self.assertEqual(["remote-model"], seen_s)

    def test_content_policy_refusal_also_switches(self):
        fo = FailoverClient(_fake([BLOCKED("blocked by safety policy")], []), "a",
                            _fake([GOOD], []), "b")
        self.assertEqual(GOOD, self._call(fo))

    def test_when_both_profiles_fail_the_chunk_is_given_up_once(self):
        seen_p, seen_s = [], []
        fo = FailoverClient(_fake([RATE_LIMITED("429")], seen_p), "a",
                            _fake([RATE_LIMITED("429")], seen_s), "b")
        self.assertEqual([], self._call(fo))
        self.assertEqual(1, len(seen_p)); self.assertEqual(1, len(seen_s))

    def test_a_plain_client_is_unchanged(self):
        seen = []
        self.assertEqual([], self._call(_fake([RATE_LIMITED("429"), GOOD], seen)))
        self.assertEqual(1, len(seen))


if __name__ == "__main__":
    unittest.main()
