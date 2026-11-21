"""Stale-Retry marker set when an approved roster changes a run's cast.

The frozen cast, checkpoint and manifest are kept for inspection; only Retry
reads this marker. Kept dependency-light so both the roster publication and the
generation route can use it without importing each other's heavier modules.
"""
from pathlib import Path

from utils import safe_load_json

RECOVERY_CAST_INVALIDATED = 'script_generation_roster_cast_invalidated'


def is_script_recovery_cast_invalidated(root):
    """True when an approved cast changed after the run whose checkpoint is kept."""
    state = safe_load_json(str(Path(root) / 'state.json'), {})
    return isinstance(state, dict) and RECOVERY_CAST_INVALIDATED in state


def get_stale_recovery_state(root, cast_sha256):
    """Return state flagged for stale Retry, or None when nothing must retire.

    The run's cast hash comes from state.json's saved options; a new approval
    that differs retires Retry until a fresh run establishes a new baseline.
    """
    state = safe_load_json(str(Path(root) / 'state.json'), {})
    if not isinstance(state, dict):
        return None
    options = state.get('script_generation_options')
    started = options.get('cast_sha256') if isinstance(options, dict) else None
    if not isinstance(started, str) or started == cast_sha256:
        return None
    state[RECOVERY_CAST_INVALIDATED] = {'run_cast_sha256': started,
                                         'approved_cast_sha256': cast_sha256}
    return state
