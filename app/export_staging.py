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

import bisect
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import secrets
import stat
import tempfile
import time

from export_staging_directory import stage_directory, stage_owner_lock, identity


MARKER = '.export-staging.json'
OWNER_LOCK = '.export-staging.owner'
MAX_MARKER_BYTES = 4096
MAX_SCAN_ENTRIES = 4096
# Where an unfinished discovery pass resumes; written only when a budget runs out.
SCAN_CURSOR = '.export-staging-scan.json'
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


def _load_scan_cursor(parent, parent_identity):
    """-> (entry name the previous call stopped after, or None; problem or None).

    A missing cursor starts from the beginning silently. An existing cursor
    that is unreadable, malformed, foreign or not a regular file also restarts
    the traversal, never widening the budget or skipping an entry, but the
    problem is returned for reporting: a cursor that keeps failing would
    otherwise bring the starvation back unseen. The read reuses descriptor
    admission, so a FIFO here cannot block recovery.
    """
    from audio_take_history import _open_regular

    root = Path(parent).resolve()
    try:
        source, _ = _open_regular(root / SCAN_CURSOR, root, MAX_MARKER_BYTES)
        with source:
            state = json.loads(source.read(MAX_MARKER_BYTES + 1))
        if (not isinstance(state, dict) or type(state.get('schema')) is not int
                or state['schema'] != 1
                or state.get('parent_identity') != list(parent_identity)
                or type(state.get('after_hex')) is not str):
            raise ValueError('stale or malformed scan cursor')
        # Names are stored as their filesystem bytes, so a name that is not
        # valid UTF-8 round-trips exactly.
        return os.fsdecode(bytes.fromhex(state['after_hex'])), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError, RecursionError, MemoryError) as error:
        return None, f'{root / SCAN_CURSOR}: scan cursor ignored, restarting ({error})'


def _save_scan_cursor(parent, parent_identity, after):
    """Persist where the next call resumes; None clears a completed pass."""
    path = Path(parent) / SCAN_CURSOR
    if after is None:
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        # Something else holds the name: preserve it and report, rather than
        # replacing a node this module did not write.
        if not stat.S_ISREG(existing.st_mode):
            raise OSError('scan cursor path is not a regular file; preserved')
    from utils import atomic_json_write
    atomic_json_write({'schema': 1, 'parent_identity': list(parent_identity),
                       'after_hex': os.fsencode(after).hex()},
                      str(path), allow_nonatomic_fallback=False)


def _get_scan_window(names, after, budget):
    """-> (names to examine this call, whether they complete a full pass).

    Names are taken in sorted order, resuming just after `after` and wrapping
    once, so a stable parent of N entries is fully examined within
    ceil(N / budget) calls. Resuming by name rather than position survives the
    cursor's own entry being removed and entries being added between calls.
    """
    ordered = sorted(names)
    start = 0 if after is None else bisect.bisect_right(ordered, after)
    rotated = ordered[start:] + ordered[:start]
    return rotated[:budget], len(rotated) <= budget


def reclaim_export_staging(parents, *, max_remove=MAX_RECLAIM,
                           max_scan=MAX_SCAN_ENTRIES):
    """Remove verifiably abandoned marked staging directories.

    Returns ``{'removed': [...], 'preserved': [...], 'errors': [...]}``. The
    scan is one level deep, over known direct children only. Its bounds:

    - Discovery: at most ``max_scan`` entries are examined (stat'ed,
      classified or admitted) per parent per call, whatever they are.
      Entry names are read and sorted first, which is proportional to the
      parent's size but costs no per-entry system call. When a parent is
      not finished, a small cursor file records where the next call
      resumes, so repeated calls reach every entry; a missing or invalid
      cursor restarts the traversal under the same budget.
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
    for parent in parents:
        parent = Path(parent)
        if os.name != 'nt' and removed < max_remove:
            from export_staging_retirement import reclaim_retired_slots
            try:
                retired = reclaim_retired_slots(parent, max_remove=max_remove - removed)
            except OSError as error:
                result['errors'].append(f'{parent}: {error}')
                continue
            for key in result:
                result[key].extend(retired[key])
            removed += len(retired['removed'])
            if removed >= max_remove:
                result['errors'].append('reclaim limit reached')
                return result
        try:
            parent_identity = identity(os.stat(parent))
            with os.scandir(parent) as entries:
                names = [entry.name for entry in entries]
        except FileNotFoundError:
            continue
        except OSError as error:
            result['errors'].append(f'{parent}: {error}')
            continue
        after, problem = _load_scan_cursor(parent, parent_identity)
        if problem:
            result['errors'].append(problem)
        window, complete = _get_scan_window(names, after, max_scan)
        examined = None
        try:
            for name in window:
                examined = name
                entry_path = os.path.join(parent, name)
                try:
                    # Windows DirEntry.stat reports zero device/inode fields.
                    # Admission requires the actual current filesystem identity.
                    info = os.stat(entry_path, follow_symlinks=False)
                except FileNotFoundError:
                    # Removed since it was listed: nothing left to reclaim.
                    continue
                except OSError as error:
                    result['errors'].append(f'{entry_path}: {error}')
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if getattr(info, 'st_file_attributes', 0) & getattr(
                        stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                    result['preserved'].append(entry_path)
                    continue
                path = Path(entry_path)
                kind, prefix = _candidate(path)
                if kind is None:
                    continue
                if ((path / OWNER_LOCK).is_symlink()
                        or (path / (OWNER_LOCK + '.lock')).is_symlink()
                        or (path / MARKER).is_symlink()):
                    result['preserved'].append(str(path))
                    continue
                marker = _read_marker(path / MARKER)
                if (marker is None or marker.get('prefix') != prefix
                        or not _matches_parent(marker, parent)):
                    result['preserved'].append(str(path))
                    continue
                authority = stage_directory(path, identity(info))
                try:
                    authority.__enter__()
                except OSError as error:
                    result['preserved'].append(str(path))
                    result['errors'].append(f'{path}: {error}')
                    continue
                lock = stage_owner_lock(authority)
                try:
                    owner_stream = lock.__enter__()
                except TimeoutError:
                    authority.__exit__(None, None, None)
                    result['preserved'].append(str(path))
                    continue
                except OSError as error:
                    authority.__exit__(None, None, None)
                    result['preserved'].append(str(path))
                    result['errors'].append(f'{path}: {error}')
                    continue
                except BaseException:
                    authority.__exit__(None, None, None)
                    raise
                try:
                    if authority.has_recovery_guard() or authority.has_rollback_backup():
                        result['preserved'].append(str(path))
                        continue
                    current = _read_marker(path / MARKER, authority=authority)
                    if current != marker or not owner_identity_matches(marker, owner_stream):
                        result['preserved'].append(str(path))
                        continue
                    def release_owner():
                        nonlocal lock
                        owned, lock = lock, None
                        if owned is not None:
                            owned.__exit__(None, None, None)
                    _cleanup_admitted(path, authority=authority, release_owner=release_owner,
                                      owner_stream=owner_stream)
                    result['removed'].append(str(path))
                    removed += 1
                except OSError as error:
                    result['preserved'].append(str(path))
                    result['errors'].append(f'{path}: {error}')
                finally:
                    try:
                        if lock is not None:
                            lock.__exit__(None, None, None)
                    finally:
                        authority.__exit__(None, None, None)
                if removed >= max_remove:
                    result['errors'].append('reclaim limit reached')
                    return result
            if not complete:
                result['errors'].append(f'{parent}: scan limit reached')
        finally:
            # Resume after the last entry actually examined, including when the
            # removal limit returns early. A call that examined nothing keeps
            # the previous cursor rather than discarding its progress.
            finished = complete and examined == (window[-1] if window else None)
            try:
                if finished:
                    _save_scan_cursor(parent, parent_identity, None)
                elif examined is not None:
                    _save_scan_cursor(parent, parent_identity, examined)
            except (OSError, ValueError) as error:
                # Losing the cursor only costs progress; it never blocks
                # recovery or the export that called it.
                result['errors'].append(f'{parent / SCAN_CURSOR}: {error}')
    return result
