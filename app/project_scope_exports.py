"""Keep large older deliveries on disk while fencing unchanged pre-scope files."""
import json
import os
from pathlib import Path
import stat
from audio_take_history import _ensure_plain_path, _version
from project_scope_files import capture_scope_file

LEGACY_EXPORTS = ('cloned_audiobook.mp3', 'audiobook.m4b', 'audacity_export.zip', 'chapter_exports')
# R2: read-along artifacts join the same pre-scope fencing. The legacy set
# stays accepted so inventories persisted before this extension remain
# interpretable instead of raising "needs inspection".
EXPORTS = LEGACY_EXPORTS + ('readalong.sidecar.json', 'readaloud.epub',
                            'readaloud.receipt.json')


def get_scope_export_inventory(root):
    root = Path(os.path.abspath(root))
    result = {}
    for name in EXPORTS:
        path = _ensure_plain_path(root/name, root)
        rows = {}
        if os.path.lexists(path):
            pending = [path]
            while pending:
                item = _ensure_plain_path(pending.pop(), root); info = item.lstat()
                if len(rows) >= 4096:
                    raise ValueError('Scope export inventory exceeds its file-count limit.')
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    rows[item.relative_to(root).as_posix()] = list(_version(info))
                elif stat.S_ISDIR(info.st_mode):
                    rows[item.relative_to(root).as_posix()] = list(_version(info))
                    pending.extend(sorted(item.iterdir()))
                else:
                    raise ValueError('Scope exports contain an unsupported file or alias.')
        result[name] = rows
    return result


def _is_supported_scope_export_inventory(inventory):
    """Purely check the producer layouts and serialized row shape.

    Supported inventories are exactly the current or legacy producer
    key sets, with row maps whose path keys are strings and whose
    versions are lists of exactly five strict ints, matching
    audio_take_history._version. Bools and floats are not ints here.
    """
    if not isinstance(inventory, dict) or set(inventory) not in (set(EXPORTS), set(LEGACY_EXPORTS)):
        return False
    for rows in inventory.values():
        if not isinstance(rows, dict):
            return False
        for path, version in rows.items():
            if not isinstance(path, str):
                return False
            if not isinstance(version, list) or len(version) != 5 or any(type(item) is not int for item in version):
                return False
    return True


def is_scope_export_inventory_compatible(recorded, current):
    """Return whether a recorded inventory still fences current exports.

    Only an exact current inventory, or an exact legacy inventory whose
    recorded legacy rows still match and whose newer exports are empty,
    is compatible. Any other key set is malformed for this purpose.
    """
    if not _is_supported_scope_export_inventory(recorded) or not _is_supported_scope_export_inventory(current):
        return False
    if set(current) != set(EXPORTS):
        return False
    keys = set(recorded)
    if keys == set(EXPORTS):
        return recorded == current
    if keys != set(LEGACY_EXPORTS):
        return False
    if any(recorded[name] != current[name] for name in LEGACY_EXPORTS):
        return False
    return all(current[name] == {} for name in EXPORTS if name not in LEGACY_EXPORTS)


def is_scope_export_historical(root, path):
    root = Path(os.path.abspath(root)); path = Path(os.path.abspath(path))
    proof, raw = capture_scope_file(root, 'state.json', include_bytes=True)
    if not proof['exists']:
        return False
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError('Invalid book state.')
    inventory = state.get('_source_scope_exports')
    if inventory is None:
        return False
    if not _is_supported_scope_export_inventory(inventory):
        raise ValueError('Scope export inventory needs inspection.')
    relative = path.relative_to(root).as_posix()
    for rows in inventory.values():
        if relative in rows and path.is_file() and list(_version(path.stat())) == rows[relative]:
            return True
    return False


def require_scope_export_current(root, path):
    if is_scope_export_historical(root, path):
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='This export predates the selected source scope. Rebuild it before downloading.')
