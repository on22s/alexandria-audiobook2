"""Stale-Retry marker set when an approved roster changes a run's cast.

The frozen cast, checkpoint and manifest are kept for inspection; only Retry
reads this marker. Kept dependency-light so both the roster publication and the
generation route can use it without importing each other's heavier modules.
"""
from pathlib import Path

from script_recovery_facts import get_saved_script_recovery_manifest, three_pass_manifest_path
from utils import safe_load_json

RECOVERY_CAST_INVALIDATED = 'script_generation_roster_cast_invalidated'
SCRIPT_PATH_NAME = 'annotated_script.json'


def is_script_recovery_cast_invalidated(root):
    """True when an approved cast changed after the run whose checkpoint is kept."""
    state = safe_load_json(str(Path(root) / 'state.json'), {})
    return isinstance(state, dict) and RECOVERY_CAST_INVALIDATED in state


def has_selected_source_recovery(root):
    """True when the selected source has a saved checkpoint Retry could resume."""
    root = Path(root)
    return get_saved_script_recovery_manifest(
        str(root), three_pass_manifest_path(str(root / SCRIPT_PATH_NAME))) is not None


def get_stale_recovery_state(root, cast_sha256):
    """Return state that must fence Retry, or None when nothing must retire.

    Only a selected-source recovery candidate can be fenced. The run's cast
    baseline comes from state.json; when the field is absent the legacy run
    cannot prove its cast, and when it is null the run was castless. Either way
    an approved cast fails closed and fences Retry until a fresh run establishes
    a new baseline. A proven, unchanged cast leaves Retry alone.
    """
    state = safe_load_json(str(Path(root) / 'state.json'), {})
    if not isinstance(state, dict) or not has_selected_source_recovery(root):
        return None
    options = state.get('script_generation_options')
    started = options.get('cast_sha256') if isinstance(options, dict) else None
    if isinstance(options, dict) and 'cast_sha256' in options and started == cast_sha256:
        return None
    state[RECOVERY_CAST_INVALIDATED] = {'run_cast_sha256': started,
                                         'approved_cast_sha256': cast_sha256}
    return state
