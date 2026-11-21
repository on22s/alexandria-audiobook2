"""Stale-Retry marker set when an approved roster changes a run's cast.

The frozen cast, checkpoint and manifest are kept for inspection; only Retry
reads this marker. Kept dependency-light so both the roster publication and the
generation route can use it without importing each other's heavier modules.
"""
from pathlib import Path

from script_recovery_facts import (get_saved_script_recovery_manifest, has_selected_source_checkpoint,
                                   has_selected_source_state, is_checkpoint_from_different_source,
                                   three_pass_manifest_path)
from utils import safe_load_json

RECOVERY_CAST_INVALIDATED = 'script_generation_roster_cast_invalidated'
RECOVERY_CAST_INVALIDATED_DETAIL = (
    'The approved cast changed since this run started, so it cannot resume; '
    'start a new run. The saved checkpoint was preserved.')
SCRIPT_PATH_NAME = 'annotated_script.json'


def get_recovery_fence_status():
    """Actionable status for a fenced recovery candidate (Start over is the cure)."""
    return {'recoverable': False, 'fenced': True, 'start_over_required': True,
            'detail': RECOVERY_CAST_INVALIDATED_DETAIL}


def is_script_recovery_cast_invalidated(root):
    """True when an approved cast changed after the run whose checkpoint is kept."""
    state = safe_load_json(str(Path(root) / 'state.json'), {})
    return isinstance(state, dict) and RECOVERY_CAST_INVALIDATED in state


def _get_prepared_text_sha256(root):
    """Digest of the selected source as the generation worker prepares it.

    state.json records the RAW upload digest, while a checkpoint fingerprint
    holds the digest of the PREPARED text (decoded, newline-normalized, front
    matter stripped). Comparing those directly calls the same book a different
    source whenever preparation changes bytes. Recompute the prepared digest
    under the saved policy so both sides of the comparison are the same stage.

    None means no comparable identity could be proved; the caller then keeps
    an ambiguous checkpoint as a potential recovery candidate instead of
    treating it as a different source.
    """
    try:
        from project_scope_source import get_scope_source_snapshot
        state = safe_load_json(str(Path(root) / 'state.json'), {})
        if not isinstance(state, dict):
            return None
        snapshot, _prepared_text = get_scope_source_snapshot(state)
        digest = snapshot.get('prepared_text_sha256')
        return digest if isinstance(digest, str) else None
    except (OSError, ValueError, RecursionError, UnicodeError, TypeError):
        return None


def has_selected_source_recovery(root):
    """True when the selected source has a saved checkpoint a launcher could resume.

    A manifest is not required: the worker checkpoints accepted pass-1 work
    before the first manifest is written. Both candidates are checked under the
    same selected-source policy so a different source or a run with no
    checkpoint at all is never fenced.
    """
    root = Path(root)
    script_path = root / SCRIPT_PATH_NAME
    selected_source_sha256 = _get_prepared_text_sha256(root)
    if (get_saved_script_recovery_manifest(
            str(root), three_pass_manifest_path(str(script_path))) is not None
            and not is_checkpoint_from_different_source(str(script_path), selected_source_sha256)):
        return True
    if not has_selected_source_state(str(root)):
        return False
    return has_selected_source_checkpoint(
        str(root), str(script_path), selected_source_sha256)


def get_fenced_recovery_status(root):
    """The cast-change recovery decision, under the caller's book lock.

    A worker checkpoint is written before the first manifest, so this must not
    depend on a manifest being present. The marker is only ever set alongside a
    recovery candidate and only cleared by a fresh run, so its presence is the
    single conservative decision every recovery surface shares: Start over is
    the cure. Reading never repairs, resumes or deletes checkpoint/manifest/cast.
    """
    if is_script_recovery_cast_invalidated(root):
        return get_recovery_fence_status()
    return None


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
