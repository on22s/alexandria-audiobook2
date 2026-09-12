"""When API retries run out on a rate limit or outage, "pause" freezes the run
for the operator instead of failing the chunk; Resume retries the request."""
import json
import os
import signal
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core
import generate_script as gs
from config_settings import LLMConfig


def _params(mode, limit=0):
    return gs.LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                           temperature=0.0, max_tokens=64, api_retry_limit=limit,
                           retry_initial_delay_seconds=0, on_api_exhaustion=mode)


def _client(script):
    """Each call pops the next item: an Exception is raised, anything else is
    returned as the JSON content."""
    items = list(script)

    def create(**_kwargs):
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(item)), finish_reason="stop")],
            usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), items


RATE_LIMITED = type("RateLimited", (Exception,), {"status_code": 429})
GOOD = [{"type": "NARRATOR", "text": "He waited."}]


@unittest.skipIf(sys.platform == "win32", "self-pause needs SIGSTOP")
class AutoPauseTests(unittest.TestCase):
    def _call(self, client, params):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, "REPO", tmp, create=True):
            return gs.call_llm_for_entries(client, "m", "sys", "user", params,
                                           log_name="t.log", label="T", max_retries=0)

    def test_pause_mode_freezes_then_retries_with_a_fresh_budget(self):
        client, remaining = _client([RATE_LIMITED("429"), GOOD])
        sent = []
        with patch.object(os, "kill", lambda pid, sig: sent.append((pid, sig))):
            out = self._call(client, _params("pause"))
        self.assertEqual([(os.getpid(), signal.SIGSTOP)], sent)
        self.assertEqual(GOOD, out)
        self.assertEqual([], remaining, "the request was retried after the pause")

    def test_fail_mode_gives_the_chunk_up_and_never_signals(self):
        client, remaining = _client([RATE_LIMITED("429"), GOOD])
        with patch.object(os, "kill", side_effect=AssertionError("must not signal")):
            out = self._call(client, _params("fail"))
        self.assertEqual([], out)
        self.assertEqual([GOOD], remaining, "nothing was retried")

    def test_content_policy_never_pauses_even_in_pause_mode(self):
        blocked = type("Blocked", (Exception,), {"status_code": 400})("blocked by safety policy")
        client, remaining = _client([blocked, GOOD])
        with patch.object(os, "kill", side_effect=AssertionError("must not signal")):
            out = self._call(client, _params("pause"))
        self.assertEqual([], out)
        self.assertEqual([GOOD], remaining)

    def test_resume_keeps_quality_feedback_and_attempt_history(self):
        client, _ = _client([GOOD, RATE_LIMITED("429"), GOOD])
        requests, attempts = [], []
        original_create = client.chat.completions.create

        def create(**kwargs):
            requests.append(kwargs)
            return original_create(**kwargs)

        client.chat.completions.create = create
        quality_calls = 0

        def validate(entries):
            nonlocal quality_calls
            quality_calls += 1
            return {"passed": quality_calls > 1,
                    "findings": [{"code": "forced_rejection", "message": "add detail"}],
                    "metrics": {}}

        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, "REPO", tmp, create=True), \
                patch.object(os, "kill", lambda *_args: None):
            out = gs.call_llm_for_entries(
                client, "m", "sys", "user", _params("pause"), log_name="t.log",
                label="T", max_retries=1, validate_entries=validate,
                attempt_observer=attempts.append)
        self.assertEqual(GOOD, out)
        self.assertEqual([1, 2, 3], [row["attempt"] for row in attempts])
        self.assertIn("Failures: add detail", requests[2]["messages"][1]["content"])

    def test_pause_for_operator_prints_the_marker_the_app_watches_for(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.object(os, "kill", lambda *a: None), redirect_stdout(buf):
            self.assertTrue(gs.pause_for_operator({"category": "rate_limited", "status_code": 429}))
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(core.AUTO_PAUSE_MARKER), lines)
        self.assertIn("rate_limited", lines[0])
        self.assertTrue(lines[1].startswith(core.AUTO_PAUSE_MARKER) and "resumed" in lines[1], lines)


class ReaderMirrorsTheMarkerTests(unittest.TestCase):
    """The parent process turns the child's marker into the task's paused
    flag - the same flag the UI's Resume button (SIGCONT) already clears."""

    def _stream(self, script):
        state = {"running": True, "logs": [], "cancel": False, "paused": False}
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = core._stream_subprocess_to_logs(
                [sys.executable, "-u", "-c", script], tmp, state, max_logs=100)
        return rc, state

    def test_marker_sets_paused_and_the_resume_line_clears_it(self):
        rc, state = self._stream(
            "print('[AUTO-PAUSE] API retries exhausted (rate_limited, HTTP 429). Paused')")
        self.assertEqual(0, rc)
        self.assertTrue(state["paused"])
        rc, state = self._stream(
            "print('[AUTO-PAUSE] API retries exhausted'); print('[AUTO-PAUSE] resumed; retrying')")
        self.assertFalse(state["paused"])

    def test_ordinary_output_leaves_paused_alone(self):
        rc, state = self._stream("print('chunk 3/40 ok')")
        self.assertFalse(state["paused"])


class FrontendAutoPauseTests(unittest.TestCase):
    def test_batch_pollers_sync_pause_button_and_prefixed_markers_notify(self):
        source_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app-core.js")
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("syncPauseButton('batch_script', state);", source)
        self.assertIn("syncPauseButton('batch_review', state);", source)
        self.assertIn("l.includes('[AUTO-PAUSE]')", source)
        self.assertIn("_autoPauseNotified[taskName] = false;", source)


class ConfigTests(unittest.TestCase):
    def test_default_is_fail_and_pause_is_accepted(self):
        base = {"base_url": "http://h/v1", "api_key": "k", "model_name": "m"}
        self.assertEqual("fail", LLMConfig(**base).on_api_exhaustion)
        self.assertEqual("pause", LLMConfig(**base, on_api_exhaustion="pause").on_api_exhaustion)

    def test_other_values_are_rejected(self):
        with self.assertRaises(Exception):
            LLMConfig(base_url="http://h/v1", api_key="k", model_name="m",
                      on_api_exhaustion="retry-forever")


if __name__ == "__main__":
    unittest.main()
