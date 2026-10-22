"""Bounded read-only file authority shared by scope preview/apply/undo."""
import hashlib
import os
from pathlib import Path, PureWindowsPath
from audio_take_history import _open_regular, _version, _ensure_plain_path, _hash, _check_cancelled

MAX_SCOPE_METADATA_BYTES = 64 * 1024**2


def capture_scope_file(root, relative_path, *, limit=MAX_SCOPE_METADATA_BYTES,
                       include_bytes=False, cancel_check=None):
    """Read one confined file; callers own project/task admission separately.

    Missing and empty files remain distinct. No file or lock is created.
    Audio callers keep include_bytes=False and use the streaming hash path.
    """
    if (not isinstance(relative_path, str) or not relative_path or '\\' in relative_path
            or '\0' in relative_path or Path(relative_path).is_absolute()
            or PureWindowsPath(relative_path).drive or '..' in Path(relative_path).parts
            or not Path(relative_path).parts or type(limit) is not int or limit < 1
            or type(include_bytes) is not bool):
        raise ValueError('Scope authority requires a confined filename and positive byte limit.')
    root = Path(os.path.abspath(root))
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root)):
        raise ValueError('Scope authority does not accept project directory aliases.')
    if not root.is_dir():
        raise ValueError('Scope authority requires an existing project directory.')
    path = _ensure_plain_path(root / relative_path, root)
    _check_cancelled(cancel_check)
    if not os.path.lexists(path):
        return {'path': relative_path, 'exists': False, 'file_version': None,
                'sha256': None, 'size': None}, None
    stream, before = _open_regular(path, root, limit, allow_empty=True)
    with stream:
        if include_bytes:
            raw = stream.read(before.st_size + 1)
            digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
        else:
            digest, size = _hash(stream, limit, cancel_check=cancel_check)
            raw = None
        _check_cancelled(cancel_check)
        if size > limit or size != before.st_size or _version(os.fstat(stream.fileno())) != _version(before):
            raise ValueError('Scope file changed while reading.')
        _ensure_plain_path(path, root)
        if _version(path.lstat()) != _version(before):
            raise ValueError('Scope file was replaced while reading.')
    return {'path': relative_path, 'exists': True, 'file_version': list(_version(before)),
            'sha256': digest, 'size': size}, raw
