"""Default-off speech retry decisions and one call-local finite attempt budget."""
import copy
import json
import math
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from audio_validation import GeneratedAudioError

MAX_PARENT_ATTEMPTS = 2
RETRY_DEADLINE_SECONDS = 600
MAX_SEED = 2**31 - 1
SEED_STEP = 1000003
OUTCOMES = frozenset(("corroborated_content_difference", "acoustic_suspicion",
    "verifier_unavailable", "provider_failure", "admission_refusal", "cancellation", "verified", "deadline"))
_BUDGET = ContextVar("speech_retry_budget", default=None)


def get_speech_retry_settings(value=None):
    """Normalize strict, off-default execution settings."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"enabled"} or type(value.get("enabled", False)) is not bool:
        raise ValueError("Speech retry settings require a boolean enabled field.")
    return {"enabled": value.get("enabled", False)}


def get_enabled_speech_retry_settings(value=None):
    settings = get_speech_retry_settings(value)
    return settings if settings["enabled"] else None


def _is_finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def get_speech_retry_decision(outcome, *, attempts_used, deadline, now, seed,
                              attempted_seeds=(), cancelled=False, inputs_current=True):
    """Return a decision from explicit values, without clocks, I/O or mutation."""
    valid_attempts = type(attempts_used) is int and 0 <= attempts_used <= MAX_PARENT_ATTEMPTS
    valid_times = _is_finite_number(deadline) and _is_finite_number(now)
    result = {"eligible": False, "reason": "invalid_contract", "remaining_budget": {
        "attempts": MAX_PARENT_ATTEMPTS - attempts_used if valid_attempts else 0,
        "seconds": min(RETRY_DEADLINE_SECONDS, max(0.0, float(deadline) - float(now))) if valid_times else 0.0},
        "proposed_seed": None}
    if (not valid_attempts or not valid_times or type(cancelled) is not bool
            or type(inputs_current) is not bool or not isinstance(outcome, str)
            or outcome not in OUTCOMES or not isinstance(attempted_seeds, (list, tuple))
            or len(attempted_seeds) > MAX_PARENT_ATTEMPTS
            or any(type(item) is not int or not 0 <= item <= MAX_SEED for item in attempted_seeds)):
        return result
    if cancelled or outcome == "cancellation":
        result["reason"] = "cancellation"
    elif not inputs_current:
        result["reason"] = "generation_inputs_changed"
    elif now >= deadline:
        result["reason"] = "deadline_expired"
    elif outcome != "corroborated_content_difference":
        result["reason"] = "not_retryable:" + outcome
    elif attempts_used != 1:
        result["reason"] = "parent_attempts_exhausted" if attempts_used else "no_attempt_recorded"
    elif type(seed) is not int or not 0 <= seed <= MAX_SEED:
        result["reason"] = "seed_unbound"
    else:
        proposed = (seed + SEED_STEP) % (MAX_SEED + 1)
        if proposed == seed or proposed in attempted_seeds:
            result["reason"] = "identical_seed"
        else:
            result.update(eligible=True, reason="corroborated_content_difference:retry_with_new_seed",
                          proposed_seed=proposed)
    return result


class SpeechRetryAttemptError(GeneratedAudioError):
    """One typed outcome; only a corroborated mismatch may schedule a retry."""

    def __init__(self, outcome, message, receipt=None):
        super().__init__(message)
        self.outcome = outcome if isinstance(outcome, str) and outcome in OUTCOMES else "admission_refusal"
        self.receipt = copy.deepcopy(receipt)


class SpeechRetryDeadlineError(SpeechRetryAttemptError):
    def __init__(self, message="Speech retry deadline expired; the prior take was kept."):
        super().__init__("deadline", message)


class SpeechRetryBudget:
    """One serial worker's budget, shared across that chunk's memory step-downs."""

    def __init__(self, *, book_token=None, uid=None, input_digest=None, initial_seed=None, deadline=None):
        deadline = time.monotonic() + RETRY_DEADLINE_SECONDS if deadline is None else deadline
        if not _is_finite_number(deadline):
            raise SpeechRetryDeadlineError("Speech retry deadline is invalid.")
        self.deadline = float(deadline)
        self.book_token = book_token
        self.uid = uid
        self.input_digest = input_digest
        self.requested_seed = initial_seed
        self.attempts_used = 0
        self.pending_attempt_id = None
        self.phase = "admission"
        self.cancel_check = None
        self._history = []

    def check_deadline(self, now=None):
        moment = time.monotonic() if now is None else now
        if not _is_finite_number(moment) or moment >= self.deadline:
            raise SpeechRetryDeadlineError()

    def begin_attempt(self, seed=None):
        self.check_active()
        if self.attempts_used >= MAX_PARENT_ATTEMPTS:
            return False
        self.attempts_used += 1
        self.pending_attempt_id = uuid.uuid4().hex
        self.phase = "admission"
        if seed is not None:
            self.requested_seed = seed
        return True

    def check_active(self):
        from audio_generation_owner import ensure_audio_generation_active, AudioGenerationCancelled
        ensure_audio_generation_active()
        if self.cancel_check is not None and self.cancel_check():
            raise AudioGenerationCancelled()
        self.check_deadline()

    def record(self, outcome, receipt=None, reason=None, seed=None):
        """Update the current attempt once; completed publication uses this snapshot."""
        if self.pending_attempt_id is None:
            return None
        if seed is not None:
            self.requested_seed = seed
        bounded_receipt = copy.deepcopy(receipt)
        if bounded_receipt is not None:
            try:
                size = len(json.dumps(bounded_receipt, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, OverflowError, RecursionError):
                size = 128 * 1024 + 1
            if size > 128 * 1024:
                raise SpeechRetryAttemptError("admission_refusal", "Speech retry evidence exceeds its bound.")
        entry = {"attempt_id": self.pending_attempt_id, "attempt": self.attempts_used,
            "outcome": outcome if isinstance(outcome, str) and outcome in OUTCOMES else "admission_refusal",
            "reason": str(reason or "")[:240], "requested_seed": self.requested_seed,
            "seed_change": "requested_not_attested", "book_token": self.book_token,
            "uid": self.uid, "input_digest": self.input_digest, "receipt": bounded_receipt}
        for position, existing in enumerate(self._history):
            if existing["attempt_id"] == self.pending_attempt_id:
                self._history[position] = entry
                return copy.deepcopy(entry)
        self._history.append(entry)
        self._history = self._history[-MAX_PARENT_ATTEMPTS:]
        return copy.deepcopy(entry)

    def get_history(self):
        return copy.deepcopy(self._history)

    def allows_memory_stepdown(self):
        return (self.attempts_used < MAX_PARENT_ATTEMPTS and bool(self._history)
                and self._history[-1]["outcome"] == "provider_failure")


@contextmanager
def ensure_speech_retry_budget(budget):
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def get_speech_retry_budget():
    return _BUDGET.get()
