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
directory. Discovery is bounded to direct children and a fixed number of
examined entries per parent, and resumes where it stopped so repeated calls
reach every entry.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import secrets
import stat
import tempfile
import time
import threading

from export_staging_directory import stage_directory, stage_owner_lock, identity


MARKER = '.export-staging.json'
OWNER_LOCK = '.export-staging.owner'
MAX_MARKER_BYTES = 4096
MAX_SCAN_ENTRIES = 4096
# An old advisory cursor, if present, is foreign evidence: never read or modify it.
SCAN_CURSOR = '.export-staging-scan.json'
MAX_OPEN_SCANS = 64
_scan_lock = threading.RLock()
_scan_iterators = {}
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
    'nested_registry': '.nested-registry-',
    'sound_delivery': 'render-',
}


class ExportStagingError(RuntimeError):
    """A staging directory could not be registered or reclaimed safely."""


@contextmanager
def _preserve_removal_failure(authority, enabled):
    try:
        yield
    except BaseException as original:
        if enabled:
            try:
                if not authority.has_recovery_guard():
                    authority.create_recovery_guard(RECOVERY)
            except OSError as guard_error:
                raise original from guard_error
        raise


def get_encoder_staging_source_root(directory):
    """Resolve worker inputs from a canonical legacy or receipt-bound stage.

    The parent retains its stage-owner lock throughout the worker lifetime.
    This read-only admission must not create missing registry metadata.
    """
    directory = Path(directory).absolute()
    if directory.resolve() != directory or not directory.is_dir():
        raise ValueError('Encoder worker requires its private canonical directory')
    from export_staging_retirement import DIRECTORY, PosixRetirement
    if re.fullmatch(r'\.pcm-encode-[A-Za-z0-9_-]+', directory.name):
        if directory.parent.parent.name == DIRECTORY:
            raise ValueError('Legacy encoder layout cannot bypass a private registry slot')
        return directory.parent
    if (os.name == 'nt' or directory.name != 'stage'
            or directory.parent.parent.name != DIRECTORY
            or re.fullmatch('[0-9a-f]{32}', directory.parent.name) is None):
        raise ValueError('Encoder worker requires its private canonical directory')
    parent = directory.parents[2]
    with PosixRetirement(parent=parent, wait=True, create=False) as transaction:
        transaction.open_slot(directory.parent.name)
        _, created = transaction.read_creation_records()
        if created is None or created['kind'] != 'encode':
            raise ValueError('Encoder worker requires an encoding stage receipt')
        with stage_directory(directory, tuple(created['stage_identity'])) as authority:
            with authority.open_owner_stream() as stream:
                transaction.validate_initialized_creation(authority, stream)
                if authority.has_recovery_guard():
                    raise ValueError('Encoder staging awaits recovery')
        transaction.check()
    return parent


class ExportStaging:
    """One marked staging directory whose lock is held by this process."""

    def __init__(self, path, kind, marker, lock, directory_identity, owner_stream,
                 *, registry_parent=None, registry_slot=None):
        self.path = Path(path)
        self.kind = kind
        self.marker = marker
        self._lock = lock
        self._owner_stream = owner_stream
        self._closed = False
        self._directory_identity = directory_identity
        self._registry_parent = registry_parent
        self._registry_slot = registry_slot

    def __enter__(self):
        return self

    def __fspath__(self):
        return str(self.path)

    def __exit__(self, kind, value, traceback):
        self.finish(remove=True)
        return False

    def finish(self, *, remove=True, preserve_on_error=False):
        """Release ownership, preserving recovery files when ``remove=False``."""
        if self._closed:
            return
        if type(preserve_on_error) is not bool:
            raise ValueError('Invalid cleanup preservation policy')
        self._closed = True
        # Prototype integration: supported-platform completion is still pending.
        released = False
        def release_owner():
            nonlocal released
            if not released:
                released = True
                self._lock.__exit__(None, None, None)
        try:
            if self._registry_slot is not None:
                from export_staging_retirement import PosixRetirement
                with stage_directory(self.path, self._directory_identity) as authority, \
                        _preserve_removal_failure(authority, preserve_on_error):
                    with PosixRetirement(parent=self._registry_parent, wait=True, create=False) as transaction:
                        transaction.open_slot(self._registry_slot)
                        if remove:
                            transaction.cleanup_initialized_creation(authority, self._owner_stream)
                        else:
                            authority.create_recovery_guard(RECOVERY)
                            try:
                                authority.unlink(MARKER)
                            except FileNotFoundError:
                                pass
                        if remove:
                            transaction.discard_empty_slot(self._registry_slot)
                return
            with stage_directory(self.path, self._directory_identity) as authority, \
                    _preserve_removal_failure(authority, preserve_on_error):
                if remove:
                    _cleanup_admitted(self.path, authority=authority, release_owner=release_owner,
                                      owner_stream=self._owner_stream)
                else:
                    try:
                        authority.unlink(MARKER)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        try:
                            authority.create_recovery_guard(RECOVERY)
                        except OSError:
                            pass
                        raise
        finally:
            release_owner()


def _cleanup_admitted(path, *, authority, release_owner, owner_stream):
    """Keep public-name removal outside the POSIX payload cleanup boundary."""
    if authority.has_recovery_guard():
        raise OSError('Export recovery guard requires preserving the staging payload')
    if os.name == 'nt':
        # Native handle deletion already addresses the admitted object. Durable
        # Windows retirement/recovery remains a separate unfinished draft step.
        _remove_contents(path, authority=authority)
        _finish_removal(path, authority=authority, release_owner=release_owner)
        return
    from export_staging_retirement import PosixRetirement
    with PosixRetirement(authority) as retirement:
        from export_staging_nested import retire_nested_registry
        retire_nested_registry(authority, retirement)
        retirement.move(owner_stream)
        retirement.cleanup_verified_stage(release_owner, owner_stream)


def _remove_contents(path, *, authority, retirement=None, nested_owner_stream=None):
    """Remove only payloads in the directory admitted by the caller."""
    if authority.has_recovery_guard():
        raise OSError('Export recovery guard requires preserving the staging payload')
    if os.name != 'nt':
        from export_staging_nested import retire_nested_registry
        from export_staging_nested_quarantine import ensure_quarantine_removal, RECEIPT
        retire_nested_registry(authority, retirement)
        with ensure_quarantine_removal(authority, nested_owner_stream) as quarantined:
            retained = (MARKER, OWNER_LOCK + '.lock', RECOVERY)
            authority.remove_contents(frozenset(retained + ((RECEIPT,) if quarantined else ())))
            if quarantined:
                # Keep the binding durable until its payload name is gone.
                os.fsync(authority.fd)
                authority.unlink(RECEIPT)
        return
    authority.remove_contents(frozenset((MARKER, OWNER_LOCK + '.lock', RECOVERY)))


def _finish_removal(path, *, authority, release_owner=lambda: None):
    # Retire the reclaimable marker while the existing owner is still locked.
    # Windows requires closing the owner stream before its file can be deleted.
    try:
        authority.unlink(MARKER)
    except FileNotFoundError:
        pass
    release_owner()
    try:
        authority.unlink(OWNER_LOCK + '.lock')
    except FileNotFoundError:
        pass
    authority.remove_empty_directory()


def _remove_tree(path, directory_identity):
    try:
        with stage_directory(path, directory_identity) as authority:
            _remove_contents(path, authority=authority)
            _finish_removal(path, authority=authority)
    except FileNotFoundError:
        pass


def _write_marker(path, marker, *, authority=None):
    encoded = json.dumps(marker, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if authority is not None:
        authority.write_marker_bytes(encoded)
        return
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


def _read_marker(path, *, authority=None):
    # Reuse descriptor admission rather than opening an untrusted marker as a
    # blocking stream (a FIFO can otherwise stop all export recovery).
    from audio_take_history import _open_regular, _version

    try:
        if authority is not None:
            raw = authority.read_marker_bytes(MAX_MARKER_BYTES)
        else:
            path = Path(os.path.abspath(path))
            # Canonicalize the trusted export parent (e.g. macOS /var), while
            # retaining the staging and marker names for alias rejection.
            root = path.parent.parent.resolve()
            path = root / path.parent.name / path.name
            source, before = _open_regular(path, root, MAX_MARKER_BYTES)
            with source:
                raw = source.read(MAX_MARKER_BYTES + 1)
                if (_version(os.fstat(source.fileno())) != _version(before)
                        or _version(path.lstat()) != _version(before)):
                    return None
        if len(raw) > MAX_MARKER_BYTES:
            return None
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        return None
    return decode_staging_marker(raw)


def decode_staging_marker(raw):
    """Validate exactly the marker bytes admitted by the caller."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MARKER_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return None
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        return None
    kind = value.get('kind')
    prefix = value.get('prefix')
    parent = value.get('parent')
    token = value.get('token')
    if (not isinstance(kind, str) or kind not in PREFIXES or prefix != PREFIXES[kind]
            or not isinstance(parent, str) or not os.path.isabs(parent)
            or not isinstance(token, str) or len(token) != 32):
        return None
    owner_identity = value.get('owner_identity')
    if owner_identity is not None and (type(owner_identity) is not list or len(owner_identity) != 2
            or any(type(number) is not int or not 0 <= number < 2**128 for number in owner_identity)):
        return None
    return value


def owner_identity_matches(marker, stream):
    # Older schema-1 markers lack this binding and retain their legacy lock
    # admission behavior. New markers bind the actual independently locked inode.
    expected = marker.get('owner_identity')
    return expected is None or expected == list(identity(os.fstat(stream.fileno())))


def create_export_staging(parent, kind):
    """Create same-filesystem staging while holding its original owner lock."""
    if os.name == 'nt':
        return _create_public_export_staging(parent, kind)
    if kind not in PREFIXES:
        raise ValueError(f'Unknown export staging kind: {kind}')
    from export_staging_retirement import PosixRetirement
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve(strict=True)
    from export_staging_nested import admit_staging_parent
    with admit_staging_parent(parent):
        with PosixRetirement(parent=parent, wait=True) as transaction:
            path, expected, marker, owner, stream = transaction.initialize_creation(kind)
            return ExportStaging(path, kind, marker, owner, expected, stream,
                                 registry_parent=parent, registry_slot=transaction.slot)


def _create_public_export_staging(parent, kind):
    """Windows allocation path; also exercises legacy public recovery in tests."""
    if kind not in PREFIXES:
        raise ValueError(f'Unknown export staging kind: {kind}')
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=PREFIXES[kind], dir=str(parent)))
    directory_identity = identity(directory.lstat())
    lock = None
    lock_entered = False
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
        with stage_directory(directory, directory_identity) as authority:
            lock = stage_owner_lock(authority, create=True)
            owner_stream = lock.__enter__()
            lock_entered = True
            marker['owner_identity'] = list(identity(os.fstat(owner_stream.fileno())))
            _write_marker(directory / MARKER, marker, authority=authority)
            authority.check()
    except BaseException as original_error:
        try:
            if lock_entered:
                lock.__exit__(None, None, None)
            _remove_tree(directory, directory_identity)
        except BaseException as cleanup_error:
            # Keep the triggering failure primary while retaining cleanup
            # diagnostics, including a replacement-directory refusal.
            raise original_error from cleanup_error
        raise
    return ExportStaging(directory, kind, marker, lock, directory_identity, owner_stream)


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


def _open_scanned_stage(path, expected_identity, parent_identity):
    authority = stage_directory(path, expected_identity)
    authority.__enter__()
    try:
        # Bind admission to the parent actually enumerated, before retirement
        # can create metadata in a replaced directory or symlink target.
        if identity(os.stat(authority.path.parent, follow_symlinks=False)) != parent_identity:
            raise OSError('Export discovery parent changed before admission')
        if os.name != 'nt' and identity(os.fstat(authority.parent_fd)) != parent_identity:
            raise OSError('Export discovery parent descriptor changed before admission')
    except BaseException:
        authority.__exit__(None, None, None)
        raise
    return authority


def _close_scan_iterators():
    with _scan_lock:
        for _, entries in _scan_iterators.values():
            entries.close()
        _scan_iterators.clear()


atexit.register(_close_scan_iterators)


def _reset_scan_iterators_after_fork():
    global _scan_lock
    # A fork may inherit the lock held by another thread. Replace it first.
    _scan_lock = threading.RLock()
    _close_scan_iterators()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_scan_iterators_after_fork)


@contextmanager
def _scan_entries(parent, budget):
    """Consume a bounded part of a serialized, identity-bound directory pass.

    A live iterator avoids re-enumerating skipped names or collecting the whole
    parent. No pathname is used to publish resume state. Unknown files remain
    untouched, including cursor files left by earlier versions.
    """
    parent = Path(os.path.abspath(parent))
    with _scan_lock:
        # Bound retained descriptors. Missing/replaced roots release their
        # handles. This maintenance examines at most MAX_OPEN_SCANS parents,
        # never their directory entries.
        for saved_parent, (saved_identity, entries) in list(_scan_iterators.items()):
            try:
                current = identity(os.stat(saved_parent))
            except OSError:
                current = None
            if current != saved_identity:
                entries.close()
                del _scan_iterators[saved_parent]
        parent_identity = identity(os.stat(parent))
        saved = _scan_iterators.get(parent)
        if saved is None:
            if len(_scan_iterators) >= MAX_OPEN_SCANS:
                oldest_parent = next(iter(_scan_iterators))
                _, oldest = _scan_iterators.pop(oldest_parent)
                oldest.close()
            entries = os.scandir(parent)
            try:
                if identity(os.stat(parent)) != parent_identity:
                    raise OSError('export parent changed while opening discovery')
            except BaseException:
                entries.close()
                raise
            _scan_iterators[parent] = (parent_identity, entries)
        else:
            _, entries = _scan_iterators.pop(parent)
            _scan_iterators[parent] = saved

        def bounded():
            for _ in range(budget):
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    _scan_iterators.pop(parent, None)
                    return
                if identity(os.stat(parent)) != parent_identity:
                    raise OSError('export parent changed during discovery')
                yield entry.name
        try:
            yield bounded()
        except BaseException:
            entries.close()
            _scan_iterators.pop(parent, None)
            raise


def reclaim_export_staging(parents, *, max_remove=MAX_RECLAIM,
                           max_scan=MAX_SCAN_ENTRIES):
    """Remove verifiably abandoned marked staging directories.

    Returns ``{'removed': [...], 'preserved': [...], 'errors': [...]}``. The
    scan is one level deep, over known direct children only. Its bounds:

    - Discovery: at most ``max_scan`` directory entries are enumerated and
      examined per parent per call, whatever their type. A serialized in-memory
      iterator continues each retained pass; a stable parent of N entries is
      covered in ceil(N / max_scan) calls while its iterator remains retained. Restarting the process restarts the bounded pass.
      At most 64 parent iterators are retained with least-recently-used eviction.
      Continuation is guaranteed while a parent is revisited before 64 other
      distinct parents; eviction restarts its bounded pass, never refuses a new
      parent. Production recovery normally visits three roots. Parent replacement,
      disappearance and iteration errors
      discard that pass safely. An EOF probe may take one additional call.
      Identity maintenance stats at most 64 retained parents per input parent and does
      not enumerate their children. No resume file is read, written or deleted.
    - Admission attempts: at most the examined candidates, so ``max_scan``
      per parent.
    - POSIX retirement slots: bounded separately by the private namespace
      (128 slots plus one reserve, refused beyond that), not by
      ``max_scan``. Each slot's recovery does its own receipt validation
      and nested cleanup. Sharing ``max_scan`` would let repeatedly
      preserved slots starve later ones, which visiting every slot avoids.
    - Removals: at most ``max_remove`` successful removals per call, across
      all parents. Attempts are bounded by the rows above, not by this.
    """
    if any(type(limit) is not int or limit < 0 for limit in (max_remove, max_scan)):
        raise ValueError('Export recovery budgets must be nonnegative integers')
    result = {'removed': [], 'preserved': [], 'errors': []}
    if max_remove == 0 or max_scan == 0:
        return result
    removed = 0
    seen_parents = set()
    for parent in parents:
        try:
            parent = Path(parent)
            scan_parent = Path(os.path.realpath(parent))
        except (OSError, RuntimeError, ValueError) as error:
            result['errors'].append(f'{parent}: {error}')
            continue
        # Preserve the caller's path spelling in result rows and diagnostics.
        # Canonical paths also bind filesystem operations to scanned identity.
        parent_key = os.path.normcase(str(scan_parent))
        if parent_key in seen_parents:
            continue
        seen_parents.add(parent_key)
        def display_path(value):
            # Filesystem operations use the admitted canonical parent; only
            # result rows use the first caller spelling for this parent.
            prefix = os.path.join(str(scan_parent), '')
            if value.startswith(prefix):
                return os.path.join(str(parent), '') + value[len(prefix):]
            if value == str(scan_parent) or value.startswith(str(scan_parent) + ':'):
                return str(parent) + value[len(str(scan_parent)):]
            return value

        if os.name != 'nt' and removed < max_remove:
            from export_staging_retirement import reclaim_retired_slots
            try:
                retired = reclaim_retired_slots(scan_parent, max_remove=max_remove - removed)
            except (OSError, RuntimeError) as error:
                result['errors'].append(f'{parent}: {error}')
                continue
            for key in result:
                result[key].extend(display_path(value) for value in retired[key])
            removed += len(retired['removed'])
            if removed >= max_remove:
                result['errors'].append('reclaim limit reached')
                return result
        try:
            with _scan_entries(scan_parent, max_scan) as window:
                parent_identity = _scan_iterators[scan_parent][0]
                for name in window:
                    entry_path = os.path.join(scan_parent, name)
                    try:
                        # Windows DirEntry.stat reports zero device/inode fields.
                        # Admission requires the actual current filesystem identity.
                        info = os.stat(entry_path, follow_symlinks=False)
                    except FileNotFoundError:
                        # Removed since it was listed: nothing left to reclaim.
                        continue
                    except OSError as error:
                        result['errors'].append(f'{display_path(entry_path)}: {error}')
                        continue
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    if getattr(info, 'st_file_attributes', 0) & getattr(
                            stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                        result['preserved'].append(display_path(entry_path))
                        continue
                    path = Path(entry_path)
                    kind, prefix = _candidate(path)
                    if kind is None:
                        continue
                    if ((path / OWNER_LOCK).is_symlink()
                            or (path / (OWNER_LOCK + '.lock')).is_symlink()
                            or (path / MARKER).is_symlink()):
                        result['preserved'].append(display_path(str(path)))
                        continue
                    marker = _read_marker(path / MARKER)
                    if (marker is None or marker.get('prefix') != prefix
                            or not _matches_parent(marker, scan_parent)):
                        result['preserved'].append(display_path(str(path)))
                        continue
                    try:
                        authority = _open_scanned_stage(path, identity(info), parent_identity)
                    except (OSError, RuntimeError) as error:
                        result['preserved'].append(display_path(str(path)))
                        result['errors'].append(f'{display_path(str(path))}: {error}')
                        continue
                    lock = stage_owner_lock(authority)
                    try:
                        owner_stream = lock.__enter__()
                    except TimeoutError:
                        authority.__exit__(None, None, None)
                        result['preserved'].append(display_path(str(path)))
                        continue
                    except OSError as error:
                        authority.__exit__(None, None, None)
                        result['preserved'].append(display_path(str(path)))
                        result['errors'].append(f'{display_path(str(path))}: {error}')
                        continue
                    except BaseException:
                        authority.__exit__(None, None, None)
                        raise
                    try:
                        if authority.has_recovery_guard() or authority.has_rollback_backup():
                            result['preserved'].append(display_path(str(path)))
                            continue
                        current = _read_marker(path / MARKER, authority=authority)
                        if current != marker or not owner_identity_matches(marker, owner_stream):
                            result['preserved'].append(display_path(str(path)))
                            continue
                        def release_owner():
                            nonlocal lock
                            owned, lock = lock, None
                            if owned is not None:
                                owned.__exit__(None, None, None)
                        _cleanup_admitted(path, authority=authority, release_owner=release_owner,
                                          owner_stream=owner_stream)
                        result['removed'].append(display_path(str(path)))
                        removed += 1
                    except (OSError, RuntimeError) as error:
                        result['preserved'].append(display_path(str(path)))
                        result['errors'].append(f'{display_path(str(path))}: {error}')
                    finally:
                        try:
                            if lock is not None:
                                lock.__exit__(None, None, None)
                        finally:
                            authority.__exit__(None, None, None)
                    if removed >= max_remove:
                        result['errors'].append('reclaim limit reached')
                        return result
                if scan_parent in _scan_iterators:
                    result['errors'].append(f'{parent}: scan limit reached')
        except FileNotFoundError:
            continue
        except OSError as error:
            result['errors'].append(f'{parent}: {error}')
    return result
