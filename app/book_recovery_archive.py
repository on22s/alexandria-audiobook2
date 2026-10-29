"""Bounded, immutable file bundles for future whole-book recovery.

Callers must inventory every required artifact and hold task and scope
admission. This module does not decide what belongs to a book, change the
active selection, restore a book, or certify audio content.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import time
import unicodedata
import uuid

from adapter_publication import sync_adapter_directory
from audio_take_history import _ensure_plain_path, _open_regular, _version, _check_cancelled
from project_scope_admission import is_scope_admitted
from utils import file_lock

ARCHIVE_DIR = '.book_recovery'
MAX_FILES = 8192
MAX_QUOTA_DEPTH = 128
MAX_LIST_RECORDS = 256
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_BOOK_STATE_BYTES = 64 * 1024**2
_ID = re.compile(r'^[0-9a-f]{32}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class ArchivePublicationUncertain(OSError):
    """A commit marker may exist; preserve its id and expected digest."""
    def __init__(self, recovery_id, manifest_sha256):
        super().__init__('Recovery publication outcome is uncertain; inspect its marker before retrying')
        self.recovery_id = recovery_id
        self.manifest_sha256 = manifest_sha256


def _require_secure_staging():
    if (os.name == 'nt' or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks
            or os.scandir not in os.supports_fd
            or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')):
        raise ValueError('Secure recovery staging is unavailable on this platform')


def _open_directory(path, *, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=dir_fd)


@contextlib.contextmanager
def _archive_lock(directory, publication=None, *, parent_fd=None):
    """Open the lock through the checked archive directory, not a swapped path."""
    directory_fd = _open_directory(ARCHIVE_DIR, dir_fd=parent_fd) if parent_fd is not None \
        else _open_directory(str(directory))
    try:
        opened = os.fstat(directory_fd)
        current = (os.stat(ARCHIVE_DIR, dir_fd=parent_fd, follow_symlinks=False)
                   if parent_fd is not None else directory.lstat())
        if (not stat.S_ISDIR(current.st_mode)
                or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) & 0o077 or
                (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('Recovery storage changed before locking')
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
        lock_fd = os.open('archive.lock', flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(lock_fd, 'a+b') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError('Recovery lock has unsafe ownership')
            with file_lock(str(directory / 'archive'), timeout=0, lock_handle=handle):
                current = (os.stat(ARCHIVE_DIR, dir_fd=parent_fd, follow_symlinks=False)
                           if parent_fd is not None else directory.lstat())
                lock_info = os.stat('archive.lock', dir_fd=directory_fd, follow_symlinks=False)
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or (info.st_dev, info.st_ino) != (lock_info.st_dev, lock_info.st_ino)
                        or not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1):
                    raise ValueError('Recovery storage changed during locking')
                body_error = None
                try:
                    yield directory_fd
                except BaseException as error:
                    body_error = error
                    raise
                finally:
                    try:
                        current = (os.stat(ARCHIVE_DIR, dir_fd=parent_fd, follow_symlinks=False)
                                   if parent_fd is not None else directory.lstat())
                        lock_info = os.stat('archive.lock', dir_fd=directory_fd,
                                            follow_symlinks=False)
                        if (not stat.S_ISDIR(current.st_mode)
                                or current.st_uid != os.getuid()
                                or stat.S_IMODE(current.st_mode) & 0o077
                                or (opened.st_dev, opened.st_ino) !=
                                   (current.st_dev, current.st_ino)
                                or (info.st_dev, info.st_ino) !=
                                   (lock_info.st_dev, lock_info.st_ino)
                                or not stat.S_ISREG(lock_info.st_mode)
                                or lock_info.st_nlink != 1
                                or lock_info.st_uid != os.getuid()
                                or stat.S_IMODE(lock_info.st_mode) & 0o077):
                            raise ValueError('Recovery storage changed while locked')
                    except (OSError, ValueError) as error:
                        # Never replace the operation's original exception. If
                        # marker publication already committed, the exit check
                        # cannot honestly report an ordinary pre-commit failure.
                        if body_error is None:
                            if publication and publication.get('committed'):
                                raise ArchivePublicationUncertain(
                                    publication['id'], publication['manifest_sha256']) from error
                            raise
    finally:
        os.close(directory_fd)


def _sync_owned_directory(fd):
    os.fsync(fd)


def _marker_bytes(recovery_id, stage_name, manifest_sha256):
    return json.dumps({'version':1, 'id':recovery_id, 'stage':stage_name,
                       'manifest_sha256':manifest_sha256}, sort_keys=True,
                      separators=(',', ':')).encode('ascii')


def _write_prepared_receipt(archive_fd, recovery_id, stage_name, manifest_sha256):
    raw = _marker_bytes(recovery_id, stage_name, manifest_sha256)
    try:
        fd = os.open('prepared-' + recovery_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=archive_fd)
    except FileExistsError:
        raise ValueError('Recovery preparation identity already exists') from None
    with os.fdopen(fd, 'wb') as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    _sync_owned_directory(archive_fd)


def _publish_marker(archive_fd, recovery_id, stage_name, manifest_sha256):
    raw = _marker_bytes(recovery_id, stage_name, manifest_sha256)
    try:
        fd = os.open(recovery_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=archive_fd)
    except FileExistsError:
        raise ValueError('Recovery archive identity already exists') from None
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        _sync_owned_directory(archive_fd)
    except BaseException as error:
        raise ArchivePublicationUncertain(recovery_id, manifest_sha256) from error


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate recovery manifest field')
        result[key] = value
    return result


def _relative_name(name):
    if (not isinstance(name, str) or not name or len(name) > 4096 or '\\' in name or '\0' in name
            or any(ord(character) < 32 or character in '<>:"|?*' for character in name)
            or Path(name).is_absolute() or PureWindowsPath(name).drive
            or '..' in Path(name).parts or not Path(name).parts
            or unicodedata.normalize('NFC', Path(name).parts[0]).casefold() == ARCHIVE_DIR.casefold()
            or Path(name).as_posix() != name):
        raise ValueError('Invalid recovery artifact path')
    parts = Path(name).parts
    if (len(name.encode('utf-8')) > 4096 or len(parts) > 128
            or any(len(part.encode('utf-8')) > 255 or ':' in part
                   or part.endswith(('.', ' ')) or PureWindowsPath(part).is_reserved()
                   for part in parts)):
        raise ValueError('Recovery artifact path is not portable')
    return name


def _validate_names(names):
    """Refuse paths that cannot coexist on supported target filesystems."""
    keys = sorted(unicodedata.normalize('NFC', name).casefold() for name in names)
    seen = set()
    for key in keys:
        parts = key.split('/')
        if key in seen or any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ValueError('Recovery artifacts have duplicate or conflicting paths')
        seen.add(key)


def _validate_portable_inventory_names(files, directories, cancel_check=None):
    """Reject aliases among directory prefixes as well as file targets."""
    _validate_names(list(files))
    normalized, seen = {}, set()
    for name in (*files, *directories):
        _check_cancelled(cancel_check)
        if name in seen:
            raise ValueError('Recovery inventory has duplicate paths')
        seen.add(name)
        parts = _relative_name(name).split('/')
        for count in range(1, len(parts) + 1):
            _check_cancelled(cancel_check)
            original = '/'.join(parts[:count])
            key = unicodedata.normalize('NFC', original).casefold()
            previous = normalized.setdefault(key, original)
            if previous != original:
                raise ValueError('Recovery inventory has conflicting portable paths')


def _bind_expected_proofs(names, proofs, cancel_check=None):
    """Detach one exact caller-captured proof for every requested file."""
    if not isinstance(proofs, list) or len(proofs) != len(names):
        raise ValueError('Recovery inventory does not match requested artifacts')
    bound = {}
    for proof in proofs:
        _check_cancelled(cancel_check)
        if (not isinstance(proof, dict) or len(proof) != 5
                or set(proof) != {'path', 'exists', 'file_version', 'sha256', 'size'}
                or proof['exists'] is not True or type(proof['size']) is not int or proof['size'] < 0
                or not isinstance(proof['sha256'], str) or not _DIGEST.fullmatch(proof['sha256'])
                or not isinstance(proof['file_version'], list) or len(proof['file_version']) != 5
                or any(type(value) is not int for value in proof['file_version'])
                or any(value < 0 for value in proof['file_version'][:3])
                or proof['file_version'][2] != proof['size']):
            raise ValueError('Invalid recovery inventory file proof')
        name = _relative_name(proof['path'])
        if name in bound:
            raise ValueError('Recovery inventory repeats a file proof')
        bound[name] = {'sha256':proof['sha256'], 'size':proof['size'],
                       'file_version':tuple(proof['file_version'])}
    if set(bound) != set(names):
        raise ValueError('Recovery inventory does not match requested artifacts')
    return bound


def _capture_book_token(root, cancel_check):
    """Compute the native book identity from a bounded, fenced state read."""
    from book_state_transaction import get_snapshot_digest
    from project_scope_files import capture_scope_file
    _proof, raw = capture_scope_file(root, 'state.json', limit=MAX_BOOK_STATE_BYTES,
                                     include_bytes=True, cancel_check=cancel_check)
    if raw is None:
        raise ValueError('Recovery requires an existing active book state')
    try:
        state = json.loads(raw, object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid book state number')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid recovery book state') from error
    if not isinstance(state, dict):
        raise ValueError('Invalid recovery book state')
    for key in ('active_book_id', 'book_generation'):
        value = state.get(key)
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError('Recovery requires a concrete active book identity')
    return get_snapshot_digest([os.path.abspath(root), state.get('active_book_id'), state.get('book_generation')])


def _direct_root(root):
    root = Path(os.path.abspath(root))
    if (not root.is_dir() or os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root))):
        raise ValueError('Recovery requires a direct project directory')
    return root


def _require_root_identity(root, root_fd, expected):
    opened = os.fstat(root_fd)
    current = root.lstat()
    if (not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode)
            or os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root))
            or (opened.st_dev, opened.st_ino) != expected
            or (current.st_dev, current.st_ino) != expected):
        raise ValueError('Recovery project root changed during capture')


def _archive_directory(root, *, create=True, root_fd=None, root_identity=None):
    root = _direct_root(root)
    directory = root / ARCHIVE_DIR
    if root_fd is not None:
        if root_identity is not None:
            _require_root_identity(root, root_fd, root_identity)
        try:
            info = os.stat(ARCHIVE_DIR, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise ValueError('Recovery storage is unavailable') from None
            try:
                os.mkdir(ARCHIVE_DIR, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            info = os.stat(ARCHIVE_DIR, dir_fd=root_fd, follow_symlinks=False)
        if root_identity is not None:
            _require_root_identity(root, root_fd, root_identity)
    else:
        if not os.path.lexists(directory):
            if not create:
                raise ValueError('Recovery storage is unavailable')
            try:
                directory.mkdir(mode=0o700)
                sync_adapter_directory(str(root))
            except FileExistsError:
                pass
        info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or os.name != 'nt' and (stat.S_IMODE(info.st_mode) & 0o077
                                    or info.st_uid != os.getuid())):
        raise ValueError('Recovery directory needs private, direct ownership')
    return directory


def _used_bytes_pinned(directory_fd, *, max_entries=MAX_FILES * 16, cancel_check=None):
    """Measure locked storage through no-follow handles with bounded descriptors."""
    total, seen = 0, 0
    root_version = _version(os.fstat(directory_fd))
    # Each node shares its parent chain, bounding memory by the entry count.
    # Reopening a chain costs extra I/O but avoids one live descriptor per depth.
    pending = [None]
    while pending:
        _check_cancelled(cancel_check)
        node = pending.pop()
        chain = []
        cursor = node
        while cursor is not None:
            chain.append(cursor)
            cursor = cursor[0]
        current_fd = _open_directory('.', dir_fd=directory_fd)
        try:
            if _version(os.fstat(current_fd)) != root_version:
                raise ValueError('Recovery storage changed during quota scan')
            for _parent, name, version, _depth in reversed(chain):
                _check_cancelled(cancel_check)
                next_fd = _open_directory(name, dir_fd=current_fd)
                try:
                    if _version(os.fstat(next_fd)) != version:
                        raise ValueError('Recovery storage changed during quota scan')
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            before = _version(os.fstat(current_fd))
            if before != (root_version if node is None else node[2]):
                raise ValueError('Recovery storage changed during quota scan')
            with os.scandir(current_fd) as entries:
                for entry in entries:
                    _check_cancelled(cancel_check)
                    seen += 1
                    if seen > max_entries:
                        raise ValueError('Recovery storage has too many entries')
                    info = os.stat(entry.name, dir_fd=current_fd, follow_symlinks=False)
                    version = _version(info)
                    if stat.S_ISDIR(info.st_mode):
                        depth = 1 if node is None else node[3] + 1
                        if depth > MAX_QUOTA_DEPTH:
                            raise ValueError('Recovery storage exceeds its directory depth limit')
                        child_fd = _open_directory(entry.name, dir_fd=current_fd)
                        try:
                            if _version(os.fstat(child_fd)) != version:
                                raise ValueError('Recovery storage changed during quota scan')
                        finally:
                            os.close(child_fd)
                        pending.append((node, entry.name, version, depth))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        total += info.st_size
                    else:
                        raise ValueError('Recovery storage contains an unsafe artifact')
                    if _version(os.stat(entry.name, dir_fd=current_fd, follow_symlinks=False)) != version:
                        raise ValueError('Recovery storage changed during quota scan')
            if _version(os.fstat(current_fd)) != before:
                raise ValueError('Recovery storage changed during quota scan')
        finally:
            os.close(current_fd)
    return total


def _used_bytes(directory, *, max_entries=MAX_FILES * 16, cancel_check=None,
                with_proofs=False, directory_fd=None, root_guard=None,
                observe_unsafe=False):
    if observe_unsafe and (not with_proofs or directory_fd is not None):
        raise ValueError('Unsafe observations require listing proofs')
    if directory_fd is not None:
        if with_proofs:
            raise ValueError('Pinned quota scan does not provide path proofs')
        return _used_bytes_pinned(directory_fd, max_entries=max_entries, cancel_check=cancel_check)
    total = 0
    seen = 0
    proofs = []
    unsafe_paths = set()
    pending = [(directory, _version(directory.lstat()), 0)]
    while pending:
        _check_cancelled(cancel_check)
        parent, expected_parent, depth = pending.pop()
        _ensure_plain_path(parent, directory)
        parent_info = parent.lstat()
        if (_version(parent_info) != expected_parent
                or getattr(parent_info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Recovery storage changed during quota scan')
        if os.name == 'nt':
            from windows_export_staging_directory import guard_windows_book_move_parents
            placeholder = parent / '.recovery-quota-scan'
            if root_guard is None:
                guard = guard_windows_book_move_parents(directory, placeholder, placeholder)
            else:
                guard = guard_windows_book_move_parents(
                    directory, placeholder, placeholder, root_guard=root_guard)
        else:
            guard = contextlib.nullcontext()
        with guard:
            if _version(parent.lstat()) != expected_parent:
                raise ValueError('Recovery storage changed during quota scan')
            for path in parent.iterdir():
                _check_cancelled(cancel_check)
                seen += 1
                if seen > max_entries:
                    raise ValueError('Recovery storage has too many entries')
                if not observe_unsafe:
                    _ensure_plain_path(path, directory)
                info = path.lstat()
                unsafe = (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not (stat.S_ISDIR(info.st_mode)
                            or stat.S_ISREG(info.st_mode) and info.st_nlink == 1))
                if unsafe and not observe_unsafe:
                    raise ValueError('Recovery storage contains an unsafe artifact')
                if not unsafe and observe_unsafe:
                    _ensure_plain_path(path, directory)
                    if _version(path.lstat()) != _version(info):
                        raise ValueError('Recovery storage changed during quota scan')
                if with_proofs:
                    proofs.append((path.relative_to(directory).as_posix(), list(_version(info))))
                if unsafe:
                    unsafe_paths.add(path.relative_to(directory).as_posix())
                    # Count known logical file bytes conservatively. Never
                    # traverse an alias or open a special file to measure it.
                    if stat.S_ISREG(info.st_mode):
                        total += info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    if depth + 1 > MAX_QUOTA_DEPTH:
                        raise ValueError('Recovery storage exceeds its directory depth limit')
                    pending.append((path, _version(info), depth + 1))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                else:
                    raise ValueError('Recovery storage contains an unsafe artifact')
            if _version(parent.lstat()) != expected_parent:
                raise ValueError('Recovery storage changed during quota scan')
    if observe_unsafe:
        return total, proofs, unsafe_paths
    return (total, proofs) if with_proofs else total


def _copy_artifact(root, name, destination, remaining, cancel_check, *, payload_fd):
    path = _ensure_plain_path(root / name, root)
    stream, before = _open_regular(path, root, remaining, allow_empty=True)
    digest = hashlib.sha256()
    count = 0
    with stream:
        descriptor = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=payload_fd)
        output = os.fdopen(descriptor, 'wb')
        with output:
            os.fchmod(output.fileno(), 0o600)
            while True:
                _check_cancelled(cancel_check)
                block = stream.read(min(1024 * 1024, remaining - count + 1))
                if not block:
                    break
                count += len(block)
                if count > remaining:
                    raise ValueError('Recovery archive exceeds its byte quota')
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
            if count != before.st_size or _version(os.fstat(stream.fileno())) != _version(before):
                raise ValueError('Recovery artifact changed during copy')
    _ensure_plain_path(path, root)
    if _version(path.lstat()) != _version(before):
        raise ValueError('Recovery artifact changed during copy')
    return {'path':name, 'size':count, 'sha256':digest.hexdigest(),
            'source_version':list(_version(before)), 'mode':stat.S_IMODE(before.st_mode)}


def _check_stage(stage, directory, identity, payload_identity):
    _ensure_plain_path(stage, directory)
    info = stage.lstat()
    payload = _ensure_plain_path(stage / 'payload', directory).lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity
            or not stat.S_ISDIR(payload.st_mode)
            or (payload.st_dev, payload.st_ino) != payload_identity
            or os.name != 'nt' and (info.st_uid != os.getuid() or payload.st_uid != os.getuid()
                                    or stat.S_IMODE(info.st_mode) & 0o077
                                    or stat.S_IMODE(payload.st_mode) & 0o077)):
        raise ValueError('Recovery staging directory changed; incomplete data was preserved')


def _require_bundle_layout(bundle, count, cancel_check=None):
    """Check exact private names with bounded enumeration, without deleting."""
    names = set()
    with os.scandir(bundle) as entries:
        for entry in entries:
            _check_cancelled(cancel_check)
            names.add(entry.name)
            if len(names) > 2:
                raise ValueError('Recovery archive contains unexpected files')
    if names != {'payload', 'manifest.json'}:
        raise ValueError('Recovery archive contains unexpected files')
    payload = _ensure_plain_path(bundle / 'payload', bundle)
    names = set()
    with os.scandir(payload) as entries:
        for entry in entries:
            _check_cancelled(cancel_check)
            names.add(entry.name)
            if len(names) > count:
                raise ValueError('Recovery archive contains unexpected payloads')
    if names != {str(index) for index in range(count)}:
        raise ValueError('Recovery archive contains unexpected payloads')


def create_recovery_archive(root, paths, *, book_token, quota_bytes, cancel_check=None,
                            expected_proofs=None, observation_revision=None,
                            _prepublication_check=None):
    root = _direct_root(root)
    _require_secure_staging()
    root_fd = _open_directory(str(root))
    try:
        opened = os.fstat(root_fd)
        identity = (opened.st_dev, opened.st_ino)
        _require_root_identity(root, root_fd, identity)
        return _create_recovery_archive_pinned(
            root, paths, book_token=book_token, quota_bytes=quota_bytes,
            cancel_check=cancel_check, expected_proofs=expected_proofs,
            observation_revision=observation_revision,
            _prepublication_check=_prepublication_check,
            _root_fd=root_fd, _root_identity=identity)
    finally:
        os.close(root_fd)


def _create_recovery_archive_pinned(root, paths, *, book_token, quota_bytes,
                                    cancel_check=None, expected_proofs=None,
                                    observation_revision=None,
                                    _prepublication_check=None,
                                    _root_fd, _root_identity):
    """Stream explicit project files to an immutable recovery bundle.

    A complete manifest is published only after every copied file is durable and
    every source version is rechecked. Existing and interrupted bundles count
    against the caller's quota. The original files are never modified.
    Optional expected proofs bind the copied bytes to an earlier inventory;
    they do not establish that the inventory contains a complete book. Windows
    inherited ACL privacy is not yet qualified for integration.
    """
    root = _direct_root(root)
    _require_root_identity(root, _root_fd, _root_identity)
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for recovery capture')
    from project_scope_files import capture_scope_file
    if not isinstance(book_token, str) or not _DIGEST.fullmatch(book_token):
        raise ValueError('Invalid recovery book identity')
    if _capture_book_token(root, cancel_check) != book_token:
        raise ValueError('Active book changed before recovery capture')
    if type(quota_bytes) is not int or quota_bytes < 1:
        raise ValueError('Recovery quota must be positive')
    if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES:
        raise ValueError('Recovery requires a bounded list of artifacts')
    if observation_revision is not None:
        if (not isinstance(observation_revision, str) or not _DIGEST.fullmatch(observation_revision)
                or expected_proofs is None or not callable(_prepublication_check)):
            raise ValueError('An observed inventory requires proofs and a publication check')
    elif _prepublication_check is not None:
        raise ValueError('A publication check requires an observed inventory')
    names = []
    for name in paths:
        _check_cancelled(cancel_check)
        names.append(_relative_name(name))
    _validate_names(names)
    expected = _bind_expected_proofs(names, expected_proofs, cancel_check) if expected_proofs is not None else None
    _check_cancelled(cancel_check)
    _require_root_identity(root, _root_fd, _root_identity)
    directory = _archive_directory(root, root_fd=_root_fd, root_identity=_root_identity)
    publication = {}
    with _archive_lock(directory, publication, parent_fd=_root_fd) as locked_archive_fd:
        _require_root_identity(root, _root_fd, _root_identity)
        used = _used_bytes(directory, cancel_check=cancel_check, directory_fd=locked_archive_fd)
        if used >= quota_bytes:
            raise ValueError('Recovery storage quota is exhausted')
        initial_versions, initial_rows, total_size = {}, [], 0
        for name in names:
            _check_cancelled(cancel_check)
            path = _ensure_plain_path(root / name, root)
            try:
                info = path.lstat()
            except FileNotFoundError as error:
                raise ValueError('Recovery artifact is unavailable') from error
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Recovery artifacts must be regular files without aliases')
            total_size += info.st_size
            if used + total_size > quota_bytes:
                raise ValueError('Recovery archive exceeds its byte quota')
            initial_versions[name] = list(_version(info))
            if expected is not None and tuple(initial_versions[name]) != expected[name]['file_version']:
                raise ValueError('Recovery artifact changed since inventory')
            initial_rows.append({'path':name, 'size':info.st_size, 'sha256':'0' * 64,
                                 'source_version':initial_versions[name], 'mode':stat.S_IMODE(info.st_mode)})
        recovery_id = uuid.uuid4().hex
        stage_name = '.pending-' + recovery_id + '-' + uuid.uuid4().hex
        created_ns = time.time_ns()
        # Hashes have a fixed serialized width, so this is the exact metadata
        # reservation for the admitted versions before any payload is copied.
        template = {'version':1, 'id':recovery_id, 'book_token':book_token,
                    'created_ns':created_ns, 'files':initial_rows, 'bytes':total_size}
        if observation_revision is not None:
            template.update(version=2, observation_revision=observation_revision, observation_complete=False)
        manifest_size = len(json.dumps(template, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode('utf-8'))
        marker_size = len(_marker_bytes(recovery_id, stage_name, '0' * 64))
        if (manifest_size > MAX_MANIFEST_BYTES
                or used + total_size + manifest_size + 2 * marker_size > quota_bytes):
            raise ValueError('Recovery archive exceeds its byte quota')
        stage = directory / stage_name
        descriptors = []
        try:
            archive_fd = os.dup(locked_archive_fd); descriptors.append(archive_fd)
            archive_stat = os.fstat(archive_fd)
            current_archive = directory.lstat()
            if (archive_stat.st_dev, archive_stat.st_ino) != (current_archive.st_dev, current_archive.st_ino):
                raise ValueError('Recovery storage changed before staging')
            os.mkdir(stage_name, mode=0o700, dir_fd=archive_fd)
            stage_fd = _open_directory(stage_name, dir_fd=archive_fd); descriptors.append(stage_fd)
            os.mkdir('payload', mode=0o700, dir_fd=stage_fd)
            payload_fd = _open_directory('payload', dir_fd=stage_fd); descriptors.append(payload_fd)
            payload = stage / 'payload'
            stage_stat, payload_stat = os.fstat(stage_fd), os.fstat(payload_fd)
            stage_id = (stage_stat.st_dev, stage_stat.st_ino)
            payload_id = (payload_stat.st_dev, payload_stat.st_ino)
            rows = []
            total = 0
            for index, name in enumerate(names):
                _check_stage(stage, directory, stage_id, payload_id)
                _require_root_identity(root, _root_fd, _root_identity)
                # Bound each copy by its reserved size as well as the quota.
                # Growth after inventory must not consume the remaining quota.
                copy_limit = min(initial_rows[index]["size"], quota_bytes - used - total)
                row = _copy_artifact(root, name, payload / str(index), copy_limit,
                                     cancel_check, payload_fd=payload_fd)
                _require_root_identity(root, _root_fd, _root_identity)
                if row['source_version'] != initial_versions[name]:
                    raise ValueError('Recovery artifact changed before copy')
                if expected is not None and (row['sha256'] != expected[name]['sha256']
                        or row['size'] != expected[name]['size']):
                    raise ValueError('Recovery artifact differs from inventory')
                total += row['size']
                rows.append(row)
            manifest = {'version':1, 'id':recovery_id, 'book_token':book_token,
                        'created_ns':created_ns, 'files':rows, 'bytes':total}
            if observation_revision is not None:
                manifest.update(version=2, observation_revision=observation_revision, observation_complete=False)
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(raw) != manifest_size or total != total_size:
                raise ValueError('Recovery reservation changed before publication')
            if len(raw) > MAX_MANIFEST_BYTES or used + total + len(raw) > quota_bytes:
                raise ValueError('Recovery archive exceeds its byte quota')
            _check_stage(stage, directory, stage_id, payload_id)
            manifest_fd = os.open('manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=stage_fd)
            with os.fdopen(manifest_fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(raw); output.flush(); os.fsync(output.fileno())
            _sync_owned_directory(payload_fd)
            _sync_owned_directory(stage_fd)
            _check_stage(stage, directory, stage_id, payload_id)
            _require_bundle_layout(stage, len(rows), cancel_check)
            staged_proofs = []
            for index, row in enumerate(rows):
                proof, _ = capture_scope_file(stage, f'payload/{index}', limit=max(1, quota_bytes),
                                               cancel_check=cancel_check)
                if proof['sha256'] != row['sha256'] or proof['size'] != row['size']:
                    raise ValueError('Recovery staging payload changed before publication')
                staged_proofs.append(proof)
            manifest_proof, staged_manifest = capture_scope_file(stage, 'manifest.json', limit=MAX_MANIFEST_BYTES,
                                                                  include_bytes=True, cancel_check=cancel_check)
            if staged_manifest != raw:
                raise ValueError('Recovery staging manifest changed before publication')
            digest = hashlib.sha256(raw).hexdigest()
            if _used_bytes(directory, cancel_check=cancel_check, directory_fd=locked_archive_fd) + 2 * len(_marker_bytes(recovery_id, stage_name, digest)) > quota_bytes:
                raise ValueError('Recovery storage exceeds its byte quota')
            for proof in staged_proofs:
                info = _ensure_plain_path(stage / proof['path'], stage).lstat()
                if info.st_nlink != 1 or list(_version(info)) != proof['file_version']:
                    raise ValueError('Recovery staging payload changed before publication')
            for row in rows:
                _check_cancelled(cancel_check)
                source = _ensure_plain_path(root / row['path'], root)
                current = source.lstat()
                if current.st_nlink != 1 or list(_version(current)) != row['source_version']:
                    raise ValueError('Recovery artifact changed before publication')
            if _capture_book_token(root, cancel_check) != book_token:
                raise ValueError('Active book changed before recovery publication')
            _check_stage(stage, directory, stage_id, payload_id)
            current_manifest = _ensure_plain_path(stage/'manifest.json', stage).lstat()
            if (current_manifest.st_nlink != 1
                    or list(_version(current_manifest)) != manifest_proof['file_version']):
                raise ValueError('Recovery staging manifest changed before publication')
            _require_bundle_layout(stage, len(rows), cancel_check)
            _check_cancelled(cancel_check)
            _write_prepared_receipt(archive_fd, recovery_id, stage_name, digest)
            _check_cancelled(cancel_check)
            if _prepublication_check is not None:
                _prepublication_check()
            _check_stage(stage, directory, stage_id, payload_id)
            _require_bundle_layout(stage, len(rows), cancel_check)
            for proof in staged_proofs:
                info = _ensure_plain_path(stage / proof['path'], stage).lstat()
                if info.st_nlink != 1 or list(_version(info)) != proof['file_version']:
                    raise ValueError('Recovery staging payload changed before publication')
            current_manifest = _ensure_plain_path(stage/'manifest.json', stage).lstat()
            if (current_manifest.st_nlink != 1
                    or list(_version(current_manifest)) != manifest_proof['file_version']):
                raise ValueError('Recovery staging manifest changed before publication')
            for row in rows:
                source = _ensure_plain_path(root / row['path'], root).lstat()
                if source.st_nlink != 1 or list(_version(source)) != row['source_version']:
                    raise ValueError('Recovery artifact changed before publication')
            if _capture_book_token(root, cancel_check) != book_token:
                raise ValueError('Active book changed before recovery publication')
            if _used_bytes(directory, cancel_check=cancel_check, directory_fd=locked_archive_fd) + len(_marker_bytes(recovery_id, stage_name, digest)) > quota_bytes:
                raise ValueError('Recovery storage exceeds its byte quota')
            _check_cancelled(cancel_check)
            if not is_scope_admitted(root):
                raise ValueError('Task and book admission ended before recovery publication')
            _require_root_identity(root, _root_fd, _root_identity)
            _publish_marker(archive_fd, recovery_id, stage_name, digest)
            publication.update(committed=True, id=recovery_id,
                               manifest_sha256=digest)
            try:
                _check_stage(stage, directory, stage_id, payload_id)
                current_archive = directory.lstat()
                if (current_archive.st_dev, current_archive.st_ino) != (archive_stat.st_dev, archive_stat.st_ino):
                    raise ValueError('Recovery storage changed after publication')
                expected_record = _marker_bytes(recovery_id, stage_name, digest)
                for record_name in (recovery_id, 'prepared-' + recovery_id):
                    _, record = capture_scope_file(directory, record_name, limit=1024,
                                                   include_bytes=True)
                    if record != expected_record:
                        raise ValueError('Recovery publication record changed after publication')
                if list(_version((stage/'manifest.json').lstat())) != manifest_proof['file_version']:
                    raise ValueError('Recovery manifest changed after publication')
                for proof in staged_proofs:
                    if list(_version((stage/proof['path']).lstat())) != proof['file_version']:
                        raise ValueError('Recovery payload changed after publication')
                # Marker publication is the commit point; do not report a
                # cancellation as though an already published record vanished.
                _require_bundle_layout(stage, len(rows))
            except (OSError, ValueError) as error:
                raise ArchivePublicationUncertain(recovery_id, digest) from error
            return {'id':recovery_id, 'files':len(rows), 'bytes':total,
                    'stage':stage_name, 'manifest_sha256':digest}
        finally:
            # A path may have been substituted after staging began. Preserve
            # incomplete data for inspection instead of deleting by pathname.
            # Pending stages count against the same bounded storage quota.
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _parse_commit_record(raw, recovery_id):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid recovery commit record') from error
    if (not isinstance(value, dict) or set(value) != {'version','id','stage','manifest_sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or value['id'] != recovery_id or not isinstance(value['manifest_sha256'], str)
            or not _DIGEST.fullmatch(value['manifest_sha256'])
            or not isinstance(value['stage'], str)
            or not re.fullmatch(r'\.pending-' + recovery_id + r'-[0-9a-f]{32}', value['stage'])):
        raise ValueError('Invalid recovery commit record identity')
    return value


def inspect_prepared_recovery(root, recovery_id):
    """Inspect preparation after a crash without claiming a restorable bundle."""
    from project_scope_files import capture_scope_file
    if not isinstance(recovery_id, str) or not _ID.fullmatch(recovery_id):
        raise ValueError('Invalid recovery archive identity')
    directory = _archive_directory(root, create=False)
    _, raw = capture_scope_file(directory, 'prepared-' + recovery_id, limit=1024, include_bytes=True)
    if raw is None:
        raise ValueError('Recovery preparation receipt is unavailable')
    prepared = _parse_commit_record(raw, recovery_id)
    _, manifest = capture_scope_file(directory, prepared['stage'] + '/manifest.json',
                                     limit=MAX_MANIFEST_BYTES, include_bytes=True)
    if manifest is None or hashlib.sha256(manifest).hexdigest() != prepared['manifest_sha256']:
        raise ValueError('Prepared recovery manifest differs from its receipt')
    _, marker_raw = capture_scope_file(directory, recovery_id, limit=1024, include_bytes=True)
    if marker_raw is None:
        marker_status = 'missing'
    else:
        try:
            marker_status = 'matches' if _parse_commit_record(marker_raw, recovery_id) == prepared else 'different'
        except ValueError:
            marker_status = 'incomplete'
    return {'id':recovery_id, 'stage':prepared['stage'],
            'manifest_sha256':prepared['manifest_sha256'], 'marker_status':marker_status}


def list_recovery_records(root, *, quota_bytes, cursor=None, limit=50,
                          expected_revision=None, cancel_check=None):
    """Report bounded archive candidates without treating names as authority.

    A candidate is not a verified or restorable book. Unrecognized or
    inconsistent objects are counted and retained; this operation never
    creates a lock, changes files, or reclaims storage. Quota is an
    observation, not permission to create or delete; writers must recheck
    under their own admission. Pass the revision when fetching later pages.
    Unsafe entries are never traversed. With any such entry, storage_bytes is
    a known logical-byte lower bound, storage_complete is false and over_quota
    is unknown (None) unless the known bytes already exceed the quota.
    """
    if type(quota_bytes) is not int or quota_bytes < 1:
        raise ValueError('Recovery quota must be positive')
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_RECORDS:
        raise ValueError('Recovery page size is invalid')
    if cursor is not None and (not isinstance(cursor, str) or not _ID.fullmatch(cursor)):
        raise ValueError('Recovery page cursor is invalid')
    if cursor is not None and expected_revision is None:
        raise ValueError('Recovery pagination requires the prior inventory revision')
    if expected_revision is not None and (not isinstance(expected_revision, str)
                                          or not _DIGEST.fullmatch(expected_revision)):
        raise ValueError('Recovery inventory revision is invalid')
    _check_cancelled(cancel_check)
    root = _direct_root(root)
    if not os.path.lexists(root / ARCHIVE_DIR):
        if expected_revision is not None:
            raise ValueError('Recovery inventory changed; start listing again')
        return {'records':[], 'unrecognized_entries':0, 'storage_bytes':0,
                'storage_complete':True, 'unsafe_entries':0,
                'quota_bytes':quota_bytes, 'over_quota':False, 'next_cursor':None,
                'revision':None, 'total_candidates':0}
    directory = _archive_directory(root, create=False)
    before = list(_version(directory.lstat()))
    if os.name == 'nt':
        from windows_export_staging_directory import WindowsBookRoot
        guard = WindowsBookRoot(directory, tuple(before[:2]))
    else:
        guard = contextlib.nullcontext()
    with guard:
        used, storage_proofs, unsafe_paths = _used_bytes(directory, cancel_check=cancel_check,
            with_proofs=True, root_guard=guard if os.name == 'nt' else None,
            observe_unsafe=True)
        if list(_version(directory.lstat())) != before:
            raise ValueError('Recovery storage changed during inspection')
        return _list_recovery_records_guarded(directory, before, used, storage_proofs,
            quota_bytes=quota_bytes, cursor=cursor, limit=limit,
            expected_revision=expected_revision, cancel_check=cancel_check,
            root_guard=guard if os.name == 'nt' else None, unsafe_paths=unsafe_paths)


def _list_recovery_records_guarded(directory, before, used, storage_proofs, *,
                                   quota_bytes, cursor, limit, expected_revision,
                                   cancel_check, root_guard=None, unsafe_paths=frozenset()):
    from project_scope_files import capture_scope_file
    by_id, unrecognized, entries = {}, 0, 0
    unsafe_roots = {name.split('/', 1)[0] for name in unsafe_paths}
    for path in directory.iterdir():
        _check_cancelled(cancel_check)
        entries += 1
        if entries > MAX_FILES * 3 + 1:
            raise ValueError('Recovery storage has too many top-level entries')
        name = path.name
        if name == 'archive.lock':
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096
                    or os.name != 'nt' and info.st_uid != os.getuid()):
                unrecognized += 1
            continue
        stage = re.fullmatch(r'\.pending-([0-9a-f]{32})-([0-9a-f]{32})', name)
        prepared = re.fullmatch(r'prepared-([0-9a-f]{32})', name)
        marker = _ID.fullmatch(name)
        identity = stage.group(1) if stage else prepared.group(1) if prepared else name if marker else None
        if identity is None:
            unrecognized += 1
            continue
        item = by_id.setdefault(identity, {'stages':[], 'prepared':False, 'marker':False, 'invalid':False})
        info = path.lstat()
        safe_type = (stat.S_ISDIR(info.st_mode) if stage else
                     stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        if os.name != 'nt':
            safe_type = bool(safe_type and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077)
        if not safe_type or name in unsafe_roots:
            item['invalid'] = True
        if stage:
            item['stages'].append(name)
        elif prepared:
            item['prepared'] = True
        else:
            item['marker'] = True
        if len(by_id) > MAX_FILES:
            raise ValueError('Recovery storage has too many candidate records')
    revision = hashlib.sha256(json.dumps({'root':before, 'entries':sorted(storage_proofs)},
        ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    if expected_revision is not None and revision != expected_revision:
        raise ValueError('Recovery inventory changed; start listing again')
    records, record_proofs = [], []
    identities = sorted(identity for identity in by_id if cursor is None or identity > cursor)
    page = identities[:limit]
    for identity in page:
        item = by_id[identity]
        _check_cancelled(cancel_check)
        row = {'id':identity, 'stage_count':len(item['stages']), 'status':'needs_inspection'}
        if not item['invalid'] and len(item['stages']) == 1 and not item['prepared'] and not item['marker']:
            row['status'] = 'uncommitted_stage'
        elif not item['invalid'] and len(item['stages']) == 1 and item['prepared']:
            try:
                prepared_proof, prepared_raw = capture_scope_file(directory, 'prepared-' + identity,
                    limit=1024, include_bytes=True, cancel_check=cancel_check)
                prepared = _parse_commit_record(prepared_raw, identity)
                record_proofs.append(prepared_proof)
                if prepared['stage'] == item['stages'][0]:
                    row['status'] = 'prepared_without_marker'
                    row['manifest_sha256'] = prepared['manifest_sha256']
                    if item['marker']:
                        # A marker exists, so an unreadable marker cannot be
                        # reported as though it were absent.
                        row['status'] = 'needs_inspection'
                        marker_proof, marker_raw = capture_scope_file(directory, identity,
                            limit=1024, include_bytes=True, cancel_check=cancel_check)
                        record_proofs.append(marker_proof)
                        try:
                            row['status'] = ('committed_unverified'
                                             if _parse_commit_record(marker_raw, identity) == prepared
                                             else 'needs_inspection')
                        except ValueError:
                            row['status'] = 'incomplete_marker'
            except (OSError, ValueError, TypeError, RecursionError):
                pass
        records.append(row)
    for proof in record_proofs:
        _check_cancelled(cancel_check)
        current = _ensure_plain_path(directory / proof['path'], directory).lstat()
        if current.st_nlink != 1 or list(_version(current)) != proof['file_version']:
            raise ValueError('Recovery record changed during inspection')
    for name, version in storage_proofs:
        _check_cancelled(cancel_check)
        path = directory / name
        if os.name == 'nt':
            from windows_export_staging_directory import guard_windows_book_move_parents
            path_guard = guard_windows_book_move_parents(
                directory, path, path, root_guard=root_guard)
        else:
            path_guard = contextlib.nullcontext()
        with path_guard:
            if name in unsafe_paths:
                _ensure_plain_path(path.parent, directory)
                current = path.lstat()
            else:
                current = _ensure_plain_path(path, directory).lstat()
            if list(_version(current)) != version:
                raise ValueError('Recovery storage changed during inspection')
    if list(_version(directory.lstat())) != before:
        raise ValueError('Recovery storage changed during inspection')
    return {'records':records, 'unrecognized_entries':unrecognized,
            'storage_bytes':used, 'storage_complete':not unsafe_paths,
            'unsafe_entries':len(unsafe_paths), 'quota_bytes':quota_bytes,
            'over_quota':True if used > quota_bytes else None if unsafe_paths else False,
            'next_cursor':page[-1] if len(identities) > len(page) else None,
            'revision':revision, 'total_candidates':len(by_id)}


def verify_recovery_archive(root, recovery_id, *, expected_manifest_sha256, quota_bytes,
                            expected_observation_revision=None, cancel_check=None):
    """Validate an existing complete bundle without changing any files.

    The expected manifest digest must come from a separately retained receipt;
    it is an integrity check, not cryptographic authentication. Files are
    checked individually, not as an atomic filesystem snapshot. A restore
    must independently copy and revalidate them under its own admission.
    """
    if not isinstance(recovery_id, str) or not _ID.fullmatch(recovery_id):
        raise ValueError('Invalid recovery archive identity')
    if not isinstance(expected_manifest_sha256, str) or not _DIGEST.fullmatch(expected_manifest_sha256):
        raise ValueError('A verified recovery manifest identity is required')
    if expected_observation_revision is not None and (
            not isinstance(expected_observation_revision, str)
            or not _DIGEST.fullmatch(expected_observation_revision)):
        raise ValueError('Expected recovery observation revision is invalid')
    if type(quota_bytes) is not int or quota_bytes < 1:
        raise ValueError('Recovery quota must be positive')
    _check_cancelled(cancel_check)
    directory = _archive_directory(root, create=False)
    before = _version(directory.lstat())
    if os.name == 'nt':
        from windows_export_staging_directory import WindowsBookRoot
        guard = WindowsBookRoot(directory, tuple(before[:2]))
    else:
        guard = contextlib.nullcontext()
    with guard:
        if _used_bytes(directory, cancel_check=cancel_check,
                       root_guard=guard if os.name == 'nt' else None) > quota_bytes:
            raise ValueError('Recovery storage exceeds its byte quota')
        if _version(directory.lstat()) != before:
            raise ValueError('Recovery storage changed during verification')
        return _verify_recovery_archive_guarded(directory, recovery_id,
            expected_manifest_sha256=expected_manifest_sha256, quota_bytes=quota_bytes,
            expected_observation_revision=expected_observation_revision,
            cancel_check=cancel_check, root_guard=guard if os.name == 'nt' else None)


def _verify_recovery_archive_guarded(directory, recovery_id, *, expected_manifest_sha256,
                                     quota_bytes, expected_observation_revision, cancel_check,
                                     root_guard=None):
    from project_scope_files import capture_scope_file
    prepared_proof, prepared_raw = capture_scope_file(directory, 'prepared-' + recovery_id,
                                                      limit=1024, include_bytes=True)
    if prepared_raw is None:
        raise ValueError('Recovery preparation receipt is unavailable')
    prepared = _parse_commit_record(prepared_raw, recovery_id)
    marker_proof, marker_raw = capture_scope_file(directory, recovery_id, limit=1024, include_bytes=True)
    if marker_raw is None:
        raise ValueError('Recovery commit marker is unavailable')
    marker = _parse_commit_record(marker_raw, recovery_id)
    if marker != prepared or marker['manifest_sha256'] != expected_manifest_sha256:
        raise ValueError('Invalid recovery commit marker identity')
    stage_name = marker['stage']
    if os.name == 'nt':
        from windows_export_staging_directory import guard_windows_book_move_parents
        payload_placeholder = directory / stage_name / 'payload' / '.recovery-verify-scan'
        nested_guard = guard_windows_book_move_parents(
            directory, payload_placeholder, payload_placeholder, root_guard=root_guard)
    else:
        nested_guard = contextlib.nullcontext()
    with nested_guard:
        result = _verify_recovery_bundle_guarded(directory, recovery_id,
            expected_manifest_sha256=expected_manifest_sha256, quota_bytes=quota_bytes,
            expected_observation_revision=expected_observation_revision,
            cancel_check=cancel_check, prepared_proof=prepared_proof,
            marker_proof=marker_proof, stage_name=stage_name)
    if _used_bytes(directory, cancel_check=cancel_check, root_guard=root_guard) > quota_bytes:
        raise ValueError('Recovery storage exceeds its byte quota')
    return result


def _verify_recovery_bundle_guarded(directory, recovery_id, *, expected_manifest_sha256,
                                    quota_bytes, expected_observation_revision, cancel_check,
                                    prepared_proof, marker_proof, stage_name):
    from project_scope_files import capture_scope_file
    bundle = _ensure_plain_path(directory / stage_name, directory)
    name = stage_name + '/manifest.json'
    proof, raw = capture_scope_file(directory, name, limit=MAX_MANIFEST_BYTES, include_bytes=True)
    if raw is None:
        raise ValueError('Recovery manifest is unavailable')
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError('Recovery manifest differs from its recorded identity')
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid recovery manifest') from error
    base_fields = {'version','id','book_token','created_ns','files','bytes'}
    if (not isinstance(manifest, dict)
            or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2)
            or set(manifest) != (base_fields if manifest['version'] == 1 else
                                 base_fields | {'observation_revision','observation_complete'})
            or (manifest['version'] == 2 and
                (not isinstance(manifest['observation_revision'], str)
                 or not _DIGEST.fullmatch(manifest['observation_revision'])
                 or manifest['observation_complete'] is not False))
            or manifest['id'] != recovery_id
            or not isinstance(manifest['book_token'], str) or not _DIGEST.fullmatch(manifest['book_token'])
            or type(manifest['created_ns']) is not int or manifest['created_ns'] < 0
            or type(manifest['bytes']) is not int or not 0 <= manifest['bytes'] <= quota_bytes
            or not isinstance(manifest['files'], list) or not 0 < len(manifest['files']) <= MAX_FILES):
        raise ValueError('Invalid recovery manifest structure')
    if (expected_observation_revision is not None and
            (manifest['version'] != 2 or manifest['observation_revision'] != expected_observation_revision)):
        raise ValueError('Recovery archive differs from the expected observation')
    names, total, payload_proofs = set(), 0, []
    for index, row in enumerate(manifest['files']):
        if (not isinstance(row, dict) or set(row) != {'path','size','sha256','source_version','mode'}
                or type(row['size']) is not int or row['size'] < 0
                or not isinstance(row['sha256'], str) or not _DIGEST.fullmatch(row['sha256'])
                or not isinstance(row['source_version'], list) or len(row['source_version']) != 5
                or any(type(item) is not int for item in row['source_version'])
                or any(item < 0 for item in row['source_version'][:3])
                or row['source_version'][2] != row['size']
                or type(row['mode']) is not int or not 0 <= row['mode'] <= 0o7777):
            raise ValueError('Invalid recovery artifact record')
        path = _relative_name(row['path'])
        if path in names:
            raise ValueError('Duplicate recovery artifact')
        names.add(path)
        total += row['size']
        if total > quota_bytes:
            raise ValueError('Recovery archive exceeds its byte quota')
        payload, _ = capture_scope_file(directory, f'{stage_name}/payload/{index}',
                                         limit=max(1, quota_bytes), cancel_check=cancel_check)
        if (not payload['exists'] or payload['size'] != row['size']
                or payload['sha256'] != row['sha256']):
            raise ValueError('Recovery payload differs from its manifest')
        payload_proofs.append(payload)
    if total != manifest['bytes'] or total + len(raw) > quota_bytes:
        raise ValueError('Recovery archive total differs from its manifest')
    _validate_names(list(names))
    _require_bundle_layout(bundle, len(manifest['files']), cancel_check)
    current, _ = capture_scope_file(directory, name, limit=MAX_MANIFEST_BYTES, cancel_check=cancel_check)
    if current != proof:
        raise ValueError('Recovery manifest changed during verification')
    current_marker, _ = capture_scope_file(directory, recovery_id, limit=1024, cancel_check=cancel_check)
    if current_marker != marker_proof:
        raise ValueError('Recovery commit marker changed during verification')
    current_prepared, _ = capture_scope_file(directory, 'prepared-' + recovery_id, limit=1024,
                                              cancel_check=cancel_check)
    if current_prepared != prepared_proof:
        raise ValueError('Recovery preparation receipt changed during verification')
    for payload in payload_proofs:
        _check_cancelled(cancel_check)
        path = _ensure_plain_path(directory / payload['path'], directory)
        current = path.lstat()
        if current.st_nlink != 1 or list(_version(current)) != payload['file_version']:
            raise ValueError('Recovery payload changed during verification')
    _require_bundle_layout(bundle, len(manifest['files']), cancel_check)
    result = {'id':recovery_id, 'book_token':manifest['book_token'], 'files':len(names), 'bytes':total,
              'manifest_sha256':hashlib.sha256(raw).hexdigest()}
    if manifest['version'] == 2:
        result.update(observation_revision=manifest['observation_revision'], observation_complete=False)
    return result
