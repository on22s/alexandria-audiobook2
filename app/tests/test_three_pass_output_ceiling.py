"""Pass 2/3 output budget: sized to the window, and never raised for a runaway.

WHAT THIS VERIFIES. A 1-entry Pass 2 window kept the full 10,000-token budget
and Qwen3-14B spent it listing 960 rows (2026-10-12); the retry then RAISED the
budget because finish_reason was "length". Here: (1) a response with more rows
than asked for is retried at the same budget, while a short one still escalates;
(2) once the run has observed a call, Pass 2/3 requests are capped by window size
plus the reasoning allowance; (3) before any observed call nothing is capped, so
a reasoning model's first request is not starved. All text is synthetic.
"""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace

import generate_script
import three_pass_generate as tp
from generate_script import LLMGenParams

FROZEN = [{"type": "SPOKEN", "text": "Wait for me!"}]
PRIOR = [{"speaker": "PIP", "text": "Wait for me!"}]


def _recording_client(content, finish_reason="stop"):
    """Every call returns `content`; records the max_tokens of each request."""
    sent = []

    def create(**kwargs):
        sent.append(kwargs["max_tokens"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason)], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


def _params(observed_calls=0, reasoning_allowance=0):
    return LLMGenParams(system_prompt="s", user_prompt_template="{batch}", max_tokens=10000,
                        temperature=0.0, observed_calls=observed_calls,
                        reasoning_allowance=reasoning_allowance)


def _runaway(rows):
    return json.dumps([{"n": i, "speaker": "PIP"} for i in range(rows)])


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class RetryPolicyTest(unittest.TestCase):
    def quality(self, expected, returned):
        return {"passed": False, "metrics": {}, "findings": [
            {"code": "alignment_violated", "message": "count",
             "expected_entries": expected, "returned_entries": returned}]}

    def test_overlong_length_finish_keeps_the_budget(self):
        self.assertEqual("retry_same_budget", generate_script.get_quality_retry_policy(
            "length", 10000, 10000, self.quality(1, 960)))

    def test_short_length_finish_still_escalates(self):
        self.assertEqual("increase_tokens", generate_script.get_quality_retry_policy(
            "length", 10000, 10000, self.quality(25, 12)))

    def test_findings_from_both_validators_carry_the_counts(self):
        from pass_quality import validate_attribution, validate_instruct
        rows = [{"n": 0, "speaker": "PIP"}, {"n": 1, "speaker": "PIP"}]
        for report in (validate_attribution(FROZEN, rows, None),
                       validate_instruct(PRIOR, [dict(r, instruct="x") for r in rows])):
            finding = report["findings"][0]
            self.assertEqual(("alignment_violated", 1, 2), (
                finding["code"], finding["expected_entries"], finding["returned_entries"]))
            self.assertTrue(generate_script.is_overlong_index_response(report))


class CeilingTest(unittest.TestCase):
    def test_unbounded_until_a_call_has_been_observed(self):
        self.assertIsNone(tp.resolve_index_output_ceiling(1, tp.ATTRIBUTE_TOKENS_PER_ENTRY, _params()))

    def test_window_size_floor_and_reasoning_allowance(self):
        cases = [(1, 0, 1024), (40, 0, 40 * tp.ATTRIBUTE_TOKENS_PER_ENTRY), (1, 3000, 4024)]
        for entries, allowance, expected in cases:
            with self.subTest(entries=entries, allowance=allowance):
                self.assertEqual(expected, tp.resolve_index_output_ceiling(
                    entries, tp.ATTRIBUTE_TOKENS_PER_ENTRY,
                    _params(observed_calls=1, reasoning_allowance=allowance)))

    def test_never_raises_a_smaller_configured_budget(self):
        params = LLMGenParams(max_tokens=500, observed_calls=1)
        self.assertEqual(500, tp.bound_index_request_params(params, 40, 64).max_tokens)


class AttributeBatchTest(unittest.TestCase):
    def run_batch(self, params, content, finish_reason="stop"):
        client, sent = _recording_client(content, finish_reason)
        try:
            _quiet(tp.attribute_batch, client, "m", FROZEN, params, roster=[], max_retries=2)
        except tp.PassExhausted:
            pass
        return sent

    def test_first_call_of_a_run_keeps_the_full_budget(self):
        self.assertEqual(10000, self.run_batch(_params(), _runaway(1))[0])

    def test_observed_run_caps_a_one_entry_window(self):
        self.assertEqual([1024], self.run_batch(_params(observed_calls=1), _runaway(1)))

    def test_runaway_retry_is_not_given_more_tokens(self):
        for params, budget in ((_params(), 10000), (_params(observed_calls=1), 1024)):
            with self.subTest(budget=budget):
                sent = self.run_batch(params, _runaway(960), finish_reason="length")
                self.assertGreaterEqual(len(sent), 2)  # it did retry
                self.assertEqual({budget}, set(sent))


class InstructBatchTest(unittest.TestCase):
    def test_observed_run_caps_a_one_entry_window(self):
        client, sent = _recording_client(json.dumps([{"n": 0, "instruct": "Urgent, breathless."}]))
        out = _quiet(tp.instruct_batch, client, "m", PRIOR, _params(observed_calls=1))
        self.assertEqual("Urgent, breathless.", out[0]["instruct"])
        self.assertEqual([1024], sent)

    def test_first_call_of_a_run_keeps_the_full_budget(self):
        client, sent = _recording_client(json.dumps([{"n": 0, "instruct": "Calm."}]))
        _quiet(tp.instruct_batch, client, "m", PRIOR, _params())
        self.assertEqual([10000], sent)


if __name__ == "__main__":
    unittest.main()
