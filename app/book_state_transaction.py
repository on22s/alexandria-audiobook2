"""Recoverable publication of the active book's flat-file artifact set."""
import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from utils import file_lock
from adapter_publication import (get_adapter_bundle_sha256, sync_adapter_directory,
                                 save_adapter_publication_bytes)

JOURNAL = '.active_book_transaction.json'


def _get_path(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Invalid book artifact path')
    path = root / relative
    if '..' in Path(relative).parts or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or path == root:
        raise ValueError('Unsafe book artifact path')
    return path


def _get_digest(path):
    if path.is_symlink():
        raise ValueError('Book artifact must not be a symlink')
    if path.is_dir():
        return get_adapter_bundle_sha256(str(path))
    if not path.is_file():
        raise ValueError('Book artifact must be a regular file or directory')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _save_journal(root, data):
    save_adapter_publication_bytes(str(root / JOURNAL), json.dumps(data).encode('utf-8'))


def _move(source, destination):
    os.replace(source, destination)
    sync_adapter_directory(str(source.parent))
    if destination.parent != source.parent:
        sync_adapter_directory(str(destination.parent))


def _finish_transaction(root, workspace):
    if workspace.exists():
        shutil.rmtree(workspace)
        sync_adapter_directory(str(root))
    (root / JOURNAL).unlink()
    sync_adapter_directory(str(root))


def recover_book_state_locked(data_dir):
    """Restore an interrupted switch; caller holds the book-operation lock."""
    root = Path(data_dir)
    journal = root / JOURNAL
    if not journal.exists():
        return False
    data = json.loads(journal.read_text())
    if data.get('version') != 1 or data.get('phase') not in ('publishing', 'recovered', 'committed'):
        raise ValueError('Invalid active-book transaction')
    name = data.get('workspace')
    if not isinstance(name, str) or not name.startswith('.book-switch-') or Path(name).name != name:
        raise ValueError('Invalid active-book workspace')
    workspace = _get_path(root, name)
    rows = data.get('entries')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Invalid active-book entries')
    targets = [_get_path(root, row['path']) for row in rows]
    if (len(set(targets)) != len(targets)
            or any(target == journal or target.is_relative_to(workspace) for target in targets)
            or any(a != b and a.is_relative_to(b) for a in targets for b in targets)):
        raise ValueError('Invalid active-book targets')
    if data['phase'] in ('recovered', 'committed'):
        _finish_transaction(root, workspace)
        return True
    # Validate every original and owned replacement before restoring anything.
    for index, (row, target) in enumerate(zip(rows, targets)):
        original = workspace / f'old-{index}'
        if row.get('existed') is True:
            candidate = original if original.exists() else target
            if not candidate.exists() or _get_digest(candidate) != row.get('old_sha256'):
                raise ValueError('Active-book original artifact is missing or changed')
        elif row.get('existed') is not False or original.exists():
            raise ValueError('Invalid active-book original ownership')
        if target.exists() and (original.exists() or not row['existed']):
            if row.get('new_sha256') is None or _get_digest(target) != row['new_sha256']:
                raise ValueError('Active-book replacement changed; refusing destructive recovery')
    for index, (row, target) in enumerate(zip(rows, targets)):
        original = workspace / f'old-{index}'
        if original.exists():
            if target.exists():
                target.unlink()  # Replacements are staged regular files, never directories.
            _move(original, target)
        elif not row['existed'] and target.exists():
            target.unlink()
            sync_adapter_directory(str(target.parent))
    data['phase'] = 'recovered'
    _save_journal(root, data)
    _finish_transaction(root, workspace)
    return True


@contextlib.contextmanager
def ensure_book_state(data_dir, timeout=10):
    """Serialize book operations and recover pending switches before use."""
    with file_lock(str(Path(data_dir) / JOURNAL), timeout=timeout):
        recover_book_state_locked(data_dir)
        yield


def apply_book_state_locked(data_dir, replacements, removals):
    """Stage and publish a complete switch; values are bytes, inputs stay unchanged."""
    root = Path(data_dir)
    if (root / JOURNAL).exists():
        raise ValueError('Recover the pending book transaction first')
    operations = list(replacements.items()) + [(name, None) for name in removals]
    targets = [_get_path(root, name) for name, _value in operations]
    if (not operations or len(set(targets)) != len(targets) or root / JOURNAL in targets
            or any(a != b and a.is_relative_to(b) for a in targets for b in targets)):
        raise ValueError('Duplicate or invalid book artifacts')
    workspace = Path(tempfile.mkdtemp(prefix='.book-switch-', dir=root))
    rows = []
    published = False
    try:
        for index, ((name, value), target) in enumerate(zip(operations, targets)):
            if value is not None and not isinstance(value, bytes):
                raise ValueError('Book replacements must be bytes')
            if value is not None and target.is_dir():
                raise ValueError('Cannot replace a directory with a book file')
            existed = target.exists()
            row = {'path':name, 'existed':existed,
                   'old_sha256':_get_digest(target) if existed else None,
                   'new_sha256':hashlib.sha256(value).hexdigest() if value is not None else None}
            if value is not None:
                mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o600
                save_adapter_publication_bytes(str(workspace / f'new-{index}'), value, mode)
            rows.append(row)
        data = {'version':1, 'phase':'publishing', 'workspace':workspace.name, 'entries':rows}
        _save_journal(root, data)
        published = True
        for index, (row, target) in enumerate(zip(rows, targets)):
            if row['existed']:
                _move(target, workspace / f'old-{index}')
            staged = workspace / f'new-{index}'
            if staged.exists():
                _move(staged, target)
        data['phase'] = 'committed'
        _save_journal(root, data)
        _finish_transaction(root, workspace)
    except BaseException:
        if published:
            recover_book_state_locked(data_dir)
        elif workspace.exists():
            shutil.rmtree(workspace)
        raise



def get_book_snapshot(data_dir, allow_missing_script=False, include_voices=True):
    """Read immutable persona inputs; caller holds the book-operation lock."""
    root = Path(data_dir)
    state_path = root / "state.json"
    state = json.loads(state_path.read_bytes()) if state_path.exists() else {}
    script_path = root / "annotated_script.json"
    script_bytes = script_path.read_bytes() if script_path.exists() or not allow_missing_script else None
    voice_path = root / "voice_config.json"
    voices = json.loads(voice_path.read_bytes()) if include_voices and voice_path.exists() else {}
    if not isinstance(state, dict) or not isinstance(voices, dict):
        raise ValueError("Book state and voices must be objects")
    identity = {key:state.get(key) for key in ("active_book_id", "input_file_path", "book_generation")}
    book_id = state.get("active_book_id") or Path(state.get("input_file_path") or "").stem or "active_book"
    return {"identity":identity, "book_id":book_id, "script_bytes":script_bytes,
            "script_sha256":hashlib.sha256(script_bytes).hexdigest() if script_bytes is not None else None, "voices":voices}


def require_book_snapshot_current(data_dir, snapshot):
    """Reject publication into a changed book; caller holds its operation lock."""
    current = get_book_snapshot(data_dir)
    if current["identity"] != snapshot["identity"] or current["script_sha256"] != snapshot["script_sha256"]:
        raise ValueError("Active book changed during persona generation; generated voices were not published")



def apply_book_input_selection(data_dir, input_path, book_id, *, timeout=10):
    """Select an input under the same book-operation contract as load/jobs."""
    from utils import safe_load_json
    with ensure_book_state(data_dir, timeout=timeout):
        state_path = Path(data_dir) / "state.json"
        with file_lock(str(state_path)):
            state = safe_load_json(str(state_path), default={})
            if not isinstance(state, dict):
                raise ValueError("Book state must be an object")
            if input_path is None:
                input_path = state.get("input_file_path")
            state.update(input_file_path=input_path, active_book_id=book_id,
                         book_generation=uuid.uuid4().hex)
            save_adapter_publication_bytes(str(state_path), json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))



def get_book_snapshot_token(snapshot):
    """Identify a book generation/source independently of editable voice fields."""
    payload = {"identity":snapshot["identity"], "script_sha256":snapshot["script_sha256"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
