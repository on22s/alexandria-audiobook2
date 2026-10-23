"""Explicit cast/roster publication with one bounded persistent exact undo.

Uses the existing task barrier, portable book/file admission and book transaction
journal. It never changes accepted script, voices, chunks, audio or take history.
"""
import base64
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from audio_take_history import _ensure_plain_path
from book_state_transaction import apply_book_state_locked
from character_roster_snapshot import (UNDO, MAX_CAST_BYTES, MAX_ROSTER_BYTES,
    get_character_roster_snapshot, get_character_roster_preview,
    get_roster_json_bytes, get_roster_digest)
from project_scope_admission import ensure_scope_admission, is_scope_admitted, _get_owner
from project_scope_files import capture_scope_file
from source_evidence import _require_keys
from utils import file_lock
import json

MAX_UNDO_BYTES = 16 * 1024**2
_admitted = ContextVar('character_roster_admission', default=())


def get_character_roster_error_detail(error, limit=300):
    """Expose bounded validation context, never OS paths or raw decode errors."""
    if isinstance(error, ValueError) and not isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return str(error)[:limit]
    return 'Character review input is unavailable or invalid'


@contextmanager
def ensure_character_roster_admission(root):
    """Exclude tasks/book changes and the legacy cast editor/build/delete writer."""
    root = Path(root).absolute()
    with ensure_scope_admission(root):
        snapshot, _source, _data, _saved = get_character_roster_snapshot(root, load_saved=False)
        cast = root / snapshot['paths']['cast']
        _ensure_plain_path(cast.parent, root).mkdir(exist_ok=True)
        lock = _ensure_plain_path(Path(str(cast) + '.lock'), root)
        if lock.exists() and (not lock.is_file() or lock.stat().st_nlink != 1):
            raise ValueError('Character review lock must be a regular owned file')
        with file_lock(cast, timeout=0):
            token = _admitted.set((*_admitted.get(), (_get_owner(root), snapshot['paths']['cast'])))
            try:
                yield
            finally:
                _admitted.reset(token)


def _require_admission(root, cast):
    if not is_scope_admitted(root) or (_get_owner(root), cast) not in _admitted.get():
        raise ValueError('Current task/book/cast admission is required for character approval or undo')


def apply_character_roster(root, proposal, expected_revision, imported_roster=None, import_id_mapping=None):
    """Recompute the reviewed proposal and publish only explicitly approved data."""
    plan = get_character_roster_preview(root, proposal, imported_roster, import_id_mapping)
    snapshot = plan['snapshot']; _require_admission(root, snapshot['paths']['cast'])
    if not isinstance(expected_revision, str) or expected_revision != plan['revision']:
        raise ValueError('Character preview changed; review the draft again')
    if snapshot['files']['undo']['exists']:
        raise ValueError('Resolve the existing exact character undo before another approval')
    if not plan['review']['can_approve']:
        raise ValueError('Every source quote needs an exact reviewed character range before approval')
    before = plan['before']
    after = {key: plan[key + '_bytes'] for key in ('cast', 'roster')}
    undo = {'schema': 1, 'book_token': snapshot['book_token'], 'source': snapshot['source'],
            'source_binding': snapshot['source_binding'],
            'before': {key: base64.b64encode(raw).decode('ascii') if raw is not None else None
                       for key, raw in before.items()},
            'before_sha256': {key: get_roster_digest(raw) for key, raw in before.items()},
            'after_sha256': {key: get_roster_digest(raw) for key, raw in after.items()}}
    undo_bytes = get_roster_json_bytes(undo, MAX_UNDO_BYTES)
    current, _source, _data, _saved = get_character_roster_snapshot(root, load_saved=False)
    if current != snapshot:
        raise ValueError('Book or source changed before character publication; review again')
    replacements = {snapshot['paths'][key]: raw for key, raw in after.items() if raw != before[key]}
    replacements[UNDO] = undo_bytes
    apply_book_state_locked(root, replacements, [])
    return {'approved': True, 'cast_changed': plan['cast_changed'], 'undo_available': True}


def _get_character_undo(root, *, require_cast_lock=True):
    if not is_scope_admitted(root):
        raise ValueError('Task/book admission is required to inspect character undo')
    proof, raw = capture_scope_file(root, UNDO, include_bytes=True, limit=MAX_UNDO_BYTES)
    if raw is None:
        raise ValueError('No character undo is available')
    undo = json.loads(raw)
    _require_keys(undo, ('schema', 'book_token', 'source', 'source_binding', 'before',
                         'before_sha256', 'after_sha256'), 'Character undo')
    if type(undo['schema']) is not int or undo['schema'] != 1:
        raise ValueError('Unsupported character undo schema')
    snapshot, _source, current, _saved = get_character_roster_snapshot(root, load_saved=False)
    if snapshot['files']['undo'] != proof:
        raise ValueError('Character undo changed while reading; inspect the preserved record again')
    if require_cast_lock:
        _require_admission(root, snapshot['paths']['cast'])
    if (undo['book_token'] != snapshot['book_token'] or undo['source'] != snapshot['source']
            or undo['source_binding'] != snapshot['source_binding']):
        raise ValueError('Book or source changed; exact character undo was preserved for inspection')
    for key in ('before', 'before_sha256', 'after_sha256'):
        _require_keys(undo[key], ('cast', 'roster'), 'Character undo bytes')
    before = {}
    for key in ('cast', 'roster'):
        if get_roster_digest(current[key]) != undo['after_sha256'][key]:
            raise ValueError('Cast or roster changed after approval; exact character undo was preserved')
        encoded = undo['before'][key]
        if encoded is not None and not isinstance(encoded, str):
            raise ValueError('Invalid saved character undo bytes')
        before[key] = base64.b64decode(encoded, validate=True) if encoded is not None else None
        if before[key] is not None and len(before[key]) > (MAX_CAST_BYTES if key == 'cast' else MAX_ROSTER_BYTES):
            raise ValueError('Character undo bytes exceed their supported limit')
        if get_roster_digest(before[key]) != undo['before_sha256'][key]:
            raise ValueError('Saved character undo bytes changed; inspect the preserved undo record')
    return proof, snapshot, before


def get_character_roster_undo_status(root):
    """Keep even stale/corrupt undo available for explicit inspection/discard."""
    if not is_scope_admitted(root):
        raise ValueError('Task/book admission is required to inspect character undo')
    try:
        proof, _ = capture_scope_file(root, UNDO, limit=MAX_UNDO_BYTES)
    except (ValueError, OSError, RecursionError) as error:
        return {'available': os.path.lexists(Path(root) / UNDO), 'can_undo': False,
                'revision': None, 'reason': get_character_roster_error_detail(error, 240)}
    if not proof['exists']:
        return {'available': False, 'can_undo': False, 'revision': None}
    try:
        checked, _snapshot, _before = _get_character_undo(root, require_cast_lock=False)
        return {'available': True, 'can_undo': True, 'revision': checked['sha256']}
    except (ValueError, OSError, RecursionError) as error:
        return {'available': True, 'can_undo': False, 'revision': proof['sha256'], 'reason': get_character_roster_error_detail(error, 240)}


def apply_character_roster_undo(root, expected_revision):
    proof, snapshot, before = _get_character_undo(root)
    if not isinstance(expected_revision, str) or proof['sha256'] != expected_revision:
        raise ValueError('Character undo changed; inspect it again')
    # Recheck the complete saved inputs immediately before compound publication.
    if get_character_roster_snapshot(root, load_saved=False)[0] != snapshot:
        raise ValueError('Character inputs changed before undo; exact undo was preserved')
    replacements = {snapshot['paths'][key]: raw for key, raw in before.items() if raw is not None}
    removals = [UNDO] + [snapshot['paths'][key] for key, raw in before.items() if raw is None]
    apply_book_state_locked(root, replacements, removals)
    return {'restored': True}


def discard_character_roster_undo(root, expected_revision):
    """Explicitly forget one exact undo record, keeping the current cast/roster."""
    if not is_scope_admitted(root):
        raise ValueError('Task/book admission is required to discard character undo')
    proof, _ = capture_scope_file(root, UNDO, limit=MAX_UNDO_BYTES)
    if not proof['exists'] or not isinstance(expected_revision, str) or expected_revision != proof['sha256']:
        raise ValueError('Character undo changed; inspect it again before discarding')
    if capture_scope_file(root, UNDO, limit=MAX_UNDO_BYTES)[0] != proof:
        raise ValueError('Character undo changed before discard; inspect the preserved record again')
    apply_book_state_locked(root, {}, [UNDO])
    return {'discarded': True}
