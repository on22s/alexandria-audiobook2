"""Pure bounded decisions; synthetic outcomes confer no publication authority."""
import copy
import json
import math
import random
import unittest
from unittest.mock import patch

from speech_retry import (MAX_PARENT_ATTEMPTS, MAX_SEED, RETRY_DEADLINE_SECONDS,
    SEED_STEP, SpeechRetryAttemptError, SpeechRetryBudget, SpeechRetryDeadlineError,
    ensure_speech_retry_budget, get_enabled_speech_retry_settings,
    get_speech_retry_budget, get_speech_retry_decision, get_speech_retry_settings)


class SpeechRetryPolicyTests(unittest.TestCase):
    def decide(self, outcome="corroborated_content_difference", **changes):
        values = dict(attempts_used=1, deadline=100.0, now=20.0, seed=17, attempted_seeds=[17])
        values.update(changes)
        return get_speech_retry_decision(outcome, **values)

    def test_only_corroborated_content_can_request_one_changed_seed(self):
        decision = self.decide()
        self.assertTrue(decision["eligible"])
        self.assertNotEqual(17, decision["proposed_seed"])
        self.assertEqual({"attempts": 1, "seconds": 80.0}, decision["remaining_budget"])
        for outcome in ("acoustic_suspicion", "verifier_unavailable", "provider_failure",
                        "admission_refusal", "cancellation", "verified", "deadline"):
            with self.subTest(outcome=outcome):
                refused = self.decide(outcome)
                self.assertFalse(refused["eligible"])
                self.assertIsNone(refused["proposed_seed"])
                self.assertIn(outcome, refused["reason"])

    def test_exhaustion_deadline_cancellation_and_changed_inputs_stop(self):
        cases = [(dict(attempts_used=0), "no_attempt_recorded"),
                 (dict(attempts_used=2), "parent_attempts_exhausted"),
                 (dict(now=100), "deadline_expired"),
                 (dict(now=101), "deadline_expired"),
                 (dict(cancelled=True), "cancellation"),
                 (dict(inputs_current=False), "generation_inputs_changed")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                result = self.decide(**changes)
                self.assertFalse(result["eligible"])
                self.assertEqual(reason, result["reason"])

    def test_already_attempted_proposed_seed_is_not_an_identical_retry(self):
        result = self.decide(attempted_seeds=[17, 17 + SEED_STEP])
        self.assertFalse(result["eligible"])
        self.assertEqual("identical_seed", result["reason"])

    def test_unknown_or_malformed_contract_never_requests_work(self):
        for outcome in (None, [], {}, True, "success", "unavailable"):
            with self.subTest(outcome=outcome):
                result = self.decide(outcome)
                self.assertFalse(result["eligible"])
                self.assertIsNone(result["proposed_seed"])
        for name in ("attempts_used", "deadline", "now", "seed"):
            for value in (True, False, None, [], {}, "1", float("nan"), float("inf"), -(10**400), 10**400):
                with self.subTest(name=name, value=repr(value)[:30]):
                    result = self.decide(**{name: value})
                    self.assertFalse(result["eligible"])
                    json.dumps(result, allow_nan=False)
        for changes in (dict(attempted_seeds=[True]), dict(attempted_seeds=[{}]),
                        dict(attempted_seeds="17"), dict(attempted_seeds=[1, 2, 3]),
                        dict(cancelled=1), dict(inputs_current=1)):
            with self.subTest(changes=changes):
                self.assertFalse(self.decide(**changes)["eligible"])

    def test_seed_boundaries_and_generated_inputs_preserve_domain_invariants(self):
        generator = random.Random(1718)
        seeds = [0, 1, MAX_SEED, MAX_SEED - SEED_STEP, MAX_SEED - SEED_STEP + 1]
        seeds.extend(generator.randrange(MAX_SEED + 1) for unused in range(1024))
        for seed in seeds:
            with self.subTest(seed=seed):
                result = self.decide(seed=seed, attempted_seeds=[seed])
                self.assertTrue(result["eligible"])
                proposed = result["proposed_seed"]
                self.assertIs(type(proposed), int)
                self.assertTrue(0 <= proposed <= MAX_SEED)
                self.assertNotEqual(seed, proposed)
                self.assertFalse(self.decide(seed=proposed, attempts_used=2,
                                            attempted_seeds=[seed, proposed])["eligible"])

    def test_decision_is_deterministic_and_does_not_read_clock_or_mutate_inputs(self):
        seeds = [17]
        before = copy.deepcopy(seeds)
        with patch("speech_retry.time.monotonic", side_effect=AssertionError("policy read a clock")):
            first = self.decide(attempted_seeds=seeds)
            second = self.decide(attempted_seeds=seeds)
        self.assertEqual(first, second)
        self.assertEqual(before, seeds)

    def test_remaining_budget_is_finite_and_bounded_even_at_large_valid_times(self):
        for now, deadline in ((0.0, 1e308), (1e308, 1e308), (100, 0), (0, 600)):
            with self.subTest(now=now, deadline=deadline):
                remaining = self.decide(now=now, deadline=deadline)["remaining_budget"]
                self.assertTrue(math.isfinite(remaining["seconds"]))
                self.assertTrue(0 <= remaining["seconds"] <= RETRY_DEADLINE_SECONDS)
                json.dumps(remaining, allow_nan=False)

    def test_budget_keeps_distinct_attempts_and_updates_one_pending_record(self):
        budget = SpeechRetryBudget(book_token="book", uid="line", input_digest="digest", deadline=100)
        receipt = {"report_sha256": "first"}
        with patch("speech_retry.time.monotonic", return_value=20):
            self.assertTrue(budget.begin_attempt(seed=17))
            budget.record("corroborated_content_difference", receipt=receipt, reason="first rejected")
            first_id = budget.get_history()[0]["attempt_id"]
            receipt["report_sha256"] = "mutated"
            self.assertTrue(budget.begin_attempt(seed=17 + SEED_STEP))
            budget.record("verified", receipt={"report_sha256": "second"})
            budget.record("admission_refusal", reason="publication fenced")
            self.assertFalse(budget.begin_attempt())
        history = budget.get_history()
        self.assertEqual(MAX_PARENT_ATTEMPTS, len(history))
        self.assertEqual("first", history[0]["receipt"]["report_sha256"])
        self.assertEqual(first_id, history[0]["attempt_id"])
        self.assertNotEqual(first_id, history[1]["attempt_id"])
        self.assertEqual("admission_refusal", history[1]["outcome"])
        history[0]["uid"] = "forged"
        self.assertEqual("line", budget.get_history()[0]["uid"])

    def test_expired_budget_does_not_start_or_create_phantom_attempt(self):
        budget = SpeechRetryBudget(deadline=100)
        with patch("speech_retry.time.monotonic", return_value=100):
            with self.assertRaises(SpeechRetryDeadlineError):
                budget.begin_attempt()
            self.assertIsNone(budget.record("deadline"))
        self.assertEqual(0, budget.attempts_used)
        self.assertEqual([], budget.get_history())

    def test_history_evidence_is_bounded_without_silently_dropping_receipt(self):
        budget = SpeechRetryBudget(deadline=100)
        with patch("speech_retry.time.monotonic", return_value=20):
            budget.begin_attempt(seed=17)
        for receipt in ({"payload": "x" * (128 * 1024)}, {"value": float("nan")}):
            with self.subTest(receipt_type=type(receipt["payload"] if "payload" in receipt else receipt["value"]).__name__):
                with self.assertRaises(SpeechRetryAttemptError):
                    budget.record("verified", receipt=receipt)
        self.assertEqual([], budget.get_history())

    def test_call_local_context_is_reset_after_failure(self):
        original = get_speech_retry_budget()
        budget = SpeechRetryBudget(deadline=100)
        with self.assertRaisesRegex(ValueError, "fixture"):
            with ensure_speech_retry_budget(budget):
                self.assertIs(budget, get_speech_retry_budget())
                raise ValueError("fixture")
        self.assertIs(original, get_speech_retry_budget())

    def test_settings_are_strict_and_disabled_by_default(self):
        self.assertEqual({"enabled": False}, get_speech_retry_settings())
        self.assertIsNone(get_enabled_speech_retry_settings())
        self.assertEqual({"enabled": True}, get_enabled_speech_retry_settings({"enabled": True}))
        for value in (True, [], "true", {"enabled": 1}, {"enabled": "true"}, {"unknown": True}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    get_speech_retry_settings(value)
