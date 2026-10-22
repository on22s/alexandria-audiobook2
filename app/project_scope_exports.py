"""Keep large older deliveries on disk while fencing unchanged pre-scope files."""
import json
import os
from pathlib import Path
import stat
from audio_take_history import _ensure_plain_path, _version
from project_scope_files import capture_scope_file

EXPORTS = ('cloned_audiobook.mp3', 'audiobook.m4b', 'audacity_export.zip', 'chapter_exports')


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
    if not isinstance(inventory, dict) or set(inventory) != set(EXPORTS):
        raise ValueError('Scope export inventory needs inspection.')
    relative = path.relative_to(root).as_posix()
    for rows in inventory.values():
        if not isinstance(rows, dict):
            raise ValueError('Invalid scope export file inventory.')
        if relative in rows and path.is_file() and list(_version(path.stat())) == rows[relative]:
            return True
    return False


def require_scope_export_current(root, path):
    if is_scope_export_historical(root, path):
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='This export predates the selected source scope. Rebuild it before downloading.')
