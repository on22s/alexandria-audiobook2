"""When the active LLM profile gives up - retries exhausted, or a content-policy
refusal - a run can switch to the other profile for the rest of the run."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import generate_script as gs
from llm_provider import FailoverClient, make_run_client
from lmstudio_settings import TokenBudgetError, get_failover_llm_config


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
SERVER_ERROR = type("ServerError", (Exception,), {"status_code": 503})
BLOCKED = type("Blocked", (Exception,), {"status_code": 400})
CONTEXT_BUDGET = type("ContextBudget", (Exception,),
                      {"status_code": 400, "body": {"code": "context_length_exceeded"}})
UNAUTHORIZED = type("Unauthorized", (Exception,), {"status_code": 401})
NOT_FOUND = type("NotFound", (Exception,), {"status_code": 404})
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
    def _params(self, api_retry_limit=0):
        return gs.LLMGenParams(system_prompt="s", user_prompt_template="{batch}", temperature=0.0,
                               max_tokens=64, api_retry_limit=api_retry_limit,
                               retry_initial_delay_seconds=0)

    def _call(self, client, params=None):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, "REPO", tmp, create=True):
            return gs.call_llm_for_entries(client, "local-model", "sys", "user", params or self._params(),
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

    def test_context_budget_never_fails_over_a_local_sizing_error(self):
        for error in (CONTEXT_BUDGET("context window exceeded"),
                      TokenBudgetError("prompt does not fit the verified context")):
            with self.subTest(error=type(error).__name__):
                seen_p, seen_s = [], []
                fo = FailoverClient(_fake([error], seen_p), "a", _fake([GOOD], seen_s), "b")
                self.assertEqual([], self._call(fo))
                self.assertFalse(fo.switched)
                self.assertEqual(1, len(seen_p))
                self.assertEqual([], seen_s, "the billed secondary must not receive the prompt")

    def test_actual_predispatch_context_budget_calls_neither_configured_provider(self):
        cfg = {'llm_failover': True, 'llm_mode': 'local',
               'llm_local': {**BASE, 'model_name': 'primary'},
               'llm_remote': {**REMOTE, 'model_name': 'secondary'}}
        seen_primary, seen_secondary = [], []
        with patch('llm_provider.make_llm_client',
                   side_effect=[_fake([], seen_primary), _fake([], seen_secondary)]):
            client = make_run_client(cfg, cfg['llm_local'], 30)
        params = self._params(api_retry_limit=3)
        params.context_length = 1
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'REPO', tmp, create=True):
            out = gs.call_llm_for_entries(client, 'primary', 'sys', 'user', params,
                                          log_name='t.log', label='T', max_retries=0)
        self.assertEqual([], out)
        self.assertEqual([], seen_primary, "the primary must not be called before sizing")
        self.assertEqual([], seen_secondary, "a local sizing failure must not reach a billed provider")
        self.assertFalse(client.switched)

    def test_auth_and_not_found_stop_retrying_but_still_fail_over(self):
        for error, category in ((UNAUTHORIZED("invalid api key"), "auth"),
                                (NOT_FOUND("model not found"), "not_found")):
            with self.subTest(category=category):
                seen_p, seen_s = [], []
                fo = FailoverClient(_fake([error], seen_p), "a", _fake([GOOD], seen_s), "b")
                self.assertEqual(GOOD, self._call(fo, self._params(api_retry_limit=3)))
                self.assertEqual(1, len(seen_p), f"{category} is deterministic: no retry budget burned")
                self.assertEqual(["b"], seen_s, "the configured secondary may have a valid key/model")

    def test_throttling_and_transient_provider_errors_keep_retrying_then_fail_over(self):
        for error in (RATE_LIMITED("429"), SERVER_ERROR("503")):
            with self.subTest(error=type(error).__name__):
                seen_p, seen_s = [], []
                fo = FailoverClient(_fake([error, error], seen_p), "a", _fake([GOOD], seen_s), "b")
                self.assertEqual(GOOD, self._call(fo, self._params(api_retry_limit=1)))
                self.assertEqual(2, len(seen_p), "transient failures still use one retry")
                self.assertEqual(["b"], seen_s)


if __name__ == "__main__":
    unittest.main()
