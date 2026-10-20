"""Per-call manual cancellation, independent of optional provider dependencies."""

from contextlib import contextmanager
from contextvars import ContextVar


_manual_cancel_requested = ContextVar("manual_cancel_requested", default=None)


class ManualRequestCancelled(Exception):
    """The owning run cancelled a manual request while waiting for a human."""


@contextmanager
def apply_manual_cancellation(cancel_requested):
    """Bind cancellation to this call context, without changing a cached client."""
    token = _manual_cancel_requested.set(cancel_requested)
    try:
        yield
    finally:
        _manual_cancel_requested.reset(token)


def ensure_manual_request_active():
    cancel_requested = _manual_cancel_requested.get()
    if cancel_requested is not None and cancel_requested():
        raise ManualRequestCancelled("Manual LLM request cancelled")
