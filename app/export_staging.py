"""Crash-recoverable, ownership-marked staging for export assembly.

Ordinary ``TemporaryDirectory`` cleanup is skipped when the process is killed.
A staging directory therefore writes a small marker and holds a kernel file
lock for as long as its owner is alive. A normal exit removes the directory; a
hard exit releases the lock and leaves the marker, so the next admitted
recovery can remove only that verifiably abandoned directory.

A directory name is never enough authority. Reclamation requires a direct
child of a known export parent, a valid marker naming the same parent and a
known prefix, and a lock that can be acquired. A live lock, missing/invalid
marker, foreign parent, symlink, unknown sibling or failed removal preserves the
directory. Discovery is bounded to direct children and a fixed entry count.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import time

from utils import file_lock


MARKER = '.export-staging.json'
OWNER_LOCK = '.export-staging.owner'
MAX_MARKER_BYTES = 4096
MAX_SCAN_ENTRIES = 4096
MAX_RECLAIM = 64
RECOVERY = '.export-staging-recovery'
PREFIXES = {
    'chapter': '.chapter-export-',
    'chapters': '.chapter-export-',
    'mp3': '.mp3-merge-',
    'm4b': '.m4b-',
    'progress': '.m4b-progress-',
    'wave': '.stream-wave-',
    'pcm': '.stream-pcm-',
    'encode': '.pcm-encode-',
    'cover': '.m4b-source-cover-',
}


class ExportStagingError(RuntimeError):
    """A staging directory could not be registered or reclaimed safely."""


class ExportStaging:
    """One marked staging directory whose lock is held by this process."""

    def __init__(self, path, kind, marker, lock):
        self.path = Path(path)
        self.kind = kind
        self.marker = marker
        self._lock = lock
        self._closed = False

    def __enter__(self):
        return self

    def __fspath__(self):
        return str(self.path)

    def __exit__(self, kind, value, traceback):
        self.finish(remove=True)
        return False

    def finish(self, *, remove=True):
        """Release ownership, preserving recovery files when ``remove=False``."""
        if self._closed:
            return
        self._closed = True
        if remove:
            # Delete payloads while the marker is still present. If this
            # fails, the marker survives for a later retry instead of
            # leaving an unidentifiable partial tree.
            try:
                _remove_contents(self.path)
            except OSError:
                self._lock.__exit__(None, None, None)
                raise
            except BaseException:
                # A programming error is not a retryable filesystem fault;
                # finish the owned bookkeeping and surface the original.
                self._lock.__exit__(None, None, None)
                try:
                    _finish_removal(self.path)
                except OSError:
                    pass
                raise
            else:
                self._lock.__exit__(None, None, None)
                _finish_removal(self.path)
        else:
            # A retained rollback/recovery directory is no longer scratch.
            # Drop the marker before releasing the lock so a reaper cannot
            # claim it; if that unlink fails, leave a recovery guard.
            try:
                os.unlink(self.path / MARKER)
            except FileNotFoundError:
                pass
            except OSError:
                try:
                    (self.path / RECOVERY).write_bytes(b'1')
                except OSError:
                    pass
            self._lock.__exit__(None, None, None)

def _remove_contents(path):
    """Remove owned payloads while leaving the marker and lock for retry."""
    for entry in os.scandir(path):
        if entry.name in (MARKER, OWNER_LOCK + '.lock', RECOVERY):
            continue
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def _finish_removal(path):
    for name in (OWNER_LOCK + '.lock', MARKER):
        try:
            os.unlink(path / name)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


def _remove_tree(path):
    try:
        _remove_contents(path)
        _finish_removal(path)
    except FileNotFoundError:
        pass


def _write_marker(path, marker):
    encoded = json.dumps(marker, sort_keys=True, separators=(',', ':')).encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('Short staging marker write')
            view = view[written:]
    finally:
        os.close(descriptor)


def _read_marker(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as source:
            raw = source.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            return None
        value = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        return None
    if not isinstance(value, dict) or value.get('schema') != 1:
        return None
    kind = value.get('kind')
    prefix = value.get('prefix')
    parent = value.get('parent')
    token = value.get('token')
    if (kind not in PREFIXES or prefix != PREFIXES[kind]
            or not isinstance(parent, str) or not os.path.isabs(parent)
            or not isinstance(token, str) or len(token) != 32):
        return None
    return value


def create_export_staging(parent, kind):
    """Create and mark a staging directory; remove it on normal context exit."""
    if kind not in PREFIXES:
        raise ValueError(f'Unknown export staging kind: {kind}')
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=PREFIXES[kind], dir=str(parent)))
    lock = file_lock(directory / OWNER_LOCK, timeout=0)
    try:
        lock.__enter__()
    except BaseException:
        _remove_tree(directory)
        raise
    marker = {
        'schema': 1,
        'kind': kind,
        'prefix': PREFIXES[kind],
        'parent': str(parent.resolve()),
        'token': secrets.token_hex(16),
        'pid': os.getpid(),
        'created_ns': time.time_ns(),
    }
    try:
        _write_marker(directory / MARKER, marker)
    except BaseException:
        lock.__exit__(None, None, None)
        _remove_tree(directory)
        raise
    return ExportStaging(directory, kind, marker, lock)


def _candidate(path):
    name = path.name
    matches = [(len(prefix), kind, prefix) for kind, prefix in PREFIXES.items()
               if name.startswith(prefix)]
    if not matches:
        return None, None
    _, kind, prefix = max(matches)
    return kind, prefix


def _matches_parent(marker, parent):
    try:
        return marker['parent'] == str(Path(parent).resolve())
    except OSError:
        return False


def reclaim_export_staging(parents, *, max_remove=MAX_RECLAIM,
                           max_scan=MAX_SCAN_ENTRIES):
    """Remove verifiably abandoned marked staging directories.

    Returns ``{'removed': [...], 'preserved': [...], 'errors': [...]}``. The
    scan is one level deep, over known direct children only, and stops at
    ``max_scan`` entries and ``max_remove`` removals.
    """
    result = {'removed': [], 'preserved': [], 'errors': []}
    removed = 0
    scanned = 0
    for parent in parents:
        parent = Path(parent)
        try:
            entries = os.scandir(parent)
        except FileNotFoundError:
            continue
        except OSError as error:
            result['errors'].append(f'{parent}: {error}')
            continue
        with entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    result['errors'].append(f'{entry.path}: {error}')
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if getattr(info, 'st_file_attributes', 0) & getattr(
                        stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                    result['preserved'].append(entry.path)
                    continue
                path = Path(entry.path)
                kind, prefix = _candidate(path)
                if kind is None:
                    continue
                scanned += 1
                if scanned > max_scan:
                    result['errors'].append(f'{parent}: scan limit reached')
                    break
                if ((path / RECOVERY).exists()
                        or (path / 'old').is_dir()
                        and any((path / 'old').iterdir())):
                    result['preserved'].append(str(path))
                    continue
                if (path / OWNER_LOCK).is_symlink() or (path / MARKER).is_symlink():
                    result['preserved'].append(str(path))
                    continue
                marker = _read_marker(path / MARKER)
                if (marker is None or marker.get('prefix') != prefix
                        or not _matches_parent(marker, parent)):
                    result['preserved'].append(str(path))
                    continue
                lock = file_lock(path / OWNER_LOCK, timeout=0)
                try:
                    lock.__enter__()
                except TimeoutError:
                    result['preserved'].append(str(path))
                    continue
                except OSError as error:
                    result['errors'].append(f'{path}: {error}')
                    continue
                try:
                    current = _read_marker(path / MARKER)
                    if current != marker:
                        result['preserved'].append(str(path))
                        continue
                    _remove_contents(path)
                    lock.__exit__(None, None, None)
                    lock = None
                    _finish_removal(path)
                    result['removed'].append(str(path))
                    removed += 1
                except OSError as error:
                    result['errors'].append(f'{path}: {error}')
                finally:
                    if lock is not None:
                        lock.__exit__(None, None, None)
                if removed >= max_remove:
                    result['errors'].append('reclaim limit reached')
                    return result
    return result
