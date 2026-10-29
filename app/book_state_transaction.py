"""Recoverable publication of the active book's flat-file artifact set."""
import contextlib
import asyncio
import errno
from contextvars import ContextVar
from dataclasses import dataclass
import threading
import hashlib
import io
import json
from editor_poll_snapshot import get_snapshot_digest
import os
import stat
import tempfile
import uuid
from pathlib import Path
from utils import file_lock
from adapter_publication import (get_adapter_bundle_sha256, sync_adapter_directory,
                                 save_adapter_publication_bytes)

JOURNAL = '.active_book_transaction.json'
MAX_BOOK_JOURNAL_BYTES = 32 * 1024**2
MAX_LEGACY_CLEANUP_ENTRIES = 65536
_held_books = ContextVar('held_book_state_scopes', default=())


def _get_book_scope_owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), task


def _capture_target_parent_identities(root_fd, relative):
    """Capture a no-follow POSIX parent chain using a bounded number of fds.

    A missing suffix is recorded as None; any later appearance is a change.
    This is evidence for a transaction, not authority to delete anything.
    """
    if (os.name == 'nt' or os.open not in os.supports_dir_fd
            or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')):
        raise OSError(errno.ENOTSUP, 'Anchored parent capture is unavailable')
    if not isinstance(relative, str):
        raise ValueError('Invalid book artifact path')
    path = Path(relative)
    if (path.is_absolute() or not path.parts
            or '..' in path.parts or len(path.parts) > 128):
        raise ValueError('Invalid book artifact path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.dup(root_fd)
    identities = []
    missing = False
    try:
        info = os.fstat(current)
        identities.append([info.st_dev, info.st_ino])
        for component in path.parts[:-1]:
            if missing:
                identities.append(None)
                continue
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                missing = True
                identities.append(None)
                continue
            os.close(current)
            current = child
            info = os.fstat(current)
            identities.append([info.st_dev, info.st_ino])
        return identities
    finally:
        os.close(current)


def _require_target_parent_identities(root_fd, relative, expected):
    """Refuse a changed or substituted parent chain without following links."""
    try:
        current = _capture_target_parent_identities(root_fd, relative)
    except OSError as error:
        if error.errno == errno.ENOTSUP:
            raise
        raise ValueError(
            'Book artifact parent changed; recovery evidence was preserved') from error
    if current != expected:
        raise ValueError('Book artifact parent changed; recovery evidence was preserved')


def _validate_target_parent_identity_evidence(names, evidence):
    """Validate persisted no-follow parent chains before using them as evidence."""
    if not isinstance(evidence, list) or len(evidence) != len(names):
        raise ValueError('Invalid active-book parent identity evidence')
    for name, chain in zip(names, evidence):
        if not isinstance(chain, list) or len(chain) != len(Path(name).parts):
            raise ValueError('Invalid active-book parent identity evidence')
        missing = False
        for index, identity in enumerate(chain):
            if identity is None:
                if index == 0:
                    raise ValueError('Invalid active-book parent identity evidence')
                missing = True
            elif (missing or not isinstance(identity, list) or len(identity) != 2
                    or any(type(value) is not int or value < 0 for value in identity)):
                raise ValueError('Invalid active-book parent identity evidence')


def _require_transaction_parent_identities(root, names, evidence, *, root_fd=None):
    """Recheck every persisted parent chain through the admitted root handle."""
    _validate_target_parent_identity_evidence(names, evidence)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.dup(root_fd) if root_fd is not None else os.open(root, flags)
    try:
        for name, expected in zip(names, evidence):
            _require_target_parent_identities(descriptor, name, expected)
    finally:
        os.close(descriptor)


@dataclass
class _BookScope:
    root: str
    owner: tuple
    identity: tuple
    active: bool = True


@dataclass(frozen=True)
class BookStateStream:
    """A caller-pinned binary source and the exact bytes it authorizes."""
    stream: object
    size: int
    sha256: str


def _check_stream_cancellation(cancel_check):
    if cancel_check is not None and cancel_check():
        from audio_generation_owner import AudioGenerationCancelled
        raise AudioGenerationCancelled()


def _direct_book_root(data_dir):
    root = Path(os.path.abspath(data_dir))
    if os.name == 'nt':
        # realpath expands harmless DOS 8.3 components (for example
        # RUNNER~1) as well as real aliases, so string equality is not a valid
        # Windows junction test. Refuse the native reparse-point attribute on
        # the root and every ancestor instead.
        ancestor = root
        while True:
            if (getattr(ancestor.lstat(), 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError('Book root must be a direct directory without aliased ancestors')
            parent = ancestor.parent
            if parent == ancestor:
                break
            ancestor = parent
    elif os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root)):
        raise ValueError('Book root must be a direct directory without aliased ancestors')
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError('Book root must be a real directory')
    return root, (info.st_dev, info.st_ino)


def _stage_book_stream(path, source, mode, cancel_check, *, on_open=None, on_finish=None, directory_fd=None):
    """Copy a declared extent without buffering it, and refuse mismatched bytes."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                         mode, dir_fd=directory_fd)
    try:
        _write_staged_stream(descriptor, source, mode, cancel_check, on_open, path)
    finally:
        if os.name == 'nt' and on_finish is not None:
            # Pin the object through the write-handle close. Windows does not
            # promise final write timestamps until that handle has closed.
            from windows_export_staging_directory import _api, _Handle, _identity
            guard = None
            try:
                guard = _Handle(_api(), path)
            except BaseException:
                os.close(descriptor)
                raise
            try:
                os.close(descriptor)
                info = Path(path).lstat()
                if (info.st_dev, info.st_ino) != _identity(guard.information()):
                    raise ValueError('Book staging file changed after writing')
                on_finish(info)
            finally:
                guard.close()
        else:
            try:
                if on_finish is not None:
                    on_finish(os.fstat(descriptor))
            finally:
                os.close(descriptor)
    if directory_fd is not None:
        os.fsync(directory_fd)
    else:
        sync_adapter_directory(str(path.parent))


def _write_staged_stream(descriptor, source, mode, cancel_check, on_open, path):
    digest, count = hashlib.sha256(), 0
    # Keep the creation descriptor open until the buffered writer has closed
    # and flushed, so the final version comes from the original file.
    with os.fdopen(descriptor, 'wb', closefd=False) as output:
        if on_open is not None:
            on_open(os.fstat(output.fileno()))
        try:
            if hasattr(os, 'fchmod'):
                os.fchmod(output.fileno(), mode)
            else:
                os.chmod(path, mode)
            while count < source.size:
                _check_stream_cancellation(cancel_check)
                requested = min(1024 * 1024, source.size - count)
                block = source.stream.read(requested)
                if not isinstance(block, bytes) or not block or len(block) > requested:
                    raise ValueError('Book stream ended early or returned invalid bytes')
                output.write(block)
                digest.update(block)
                count += len(block)
            _check_stream_cancellation(cancel_check)
            trailing = source.stream.read(1)
            if not isinstance(trailing, bytes) or trailing:
                raise ValueError('Book stream exceeds its declared size')
            if digest.hexdigest() != source.sha256:
                raise ValueError('Book stream differs from its declared identity')
            output.flush()
            os.fsync(output.fileno())
        finally:
            # Windows may not expose the stable named file identity until the
            # first writes have completed. Refresh ownership while the same
            # exclusive creation handle is still open, including on failure.
            if on_open is not None:
                on_open(os.fstat(output.fileno()))


def _book_info_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def _book_target_version(path):
    return _book_info_version(path.lstat())


class _BookScanBudget:
    def __init__(self, byte_limit, entry_limit, cancel_check):
        self.byte_limit, self.entry_limit = byte_limit, entry_limit
        self.bytes, self.entries, self.cancel_check = 0, 0, cancel_check

    def entry(self):
        _check_stream_cancellation(self.cancel_check)
        self.entries += 1
        if self.entries > self.entry_limit:
            raise ValueError('Book destination scan exceeds its entry limit')

    def file(self, size):
        _check_stream_cancellation(self.cancel_check)
        self.bytes += size
        if self.bytes > self.byte_limit:
            raise ValueError('Book destination scan exceeds its byte limit')


def _bounded_book_file_digest(path, budget):
    before = _book_target_version(path)
    if not stat.S_ISREG(before[5]):
        raise ValueError('Book destination is not a regular file')
    budget.file(before[2])
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                opened.st_ctime_ns, opened.st_mode, opened.st_nlink) != before:
            raise ValueError('Book destination changed during scan')
        while True:
            _check_stream_cancellation(budget.cancel_check)
            block = handle.read(min(1024 * 1024, before[2] - count + 1))
            if not block:
                break
            count += len(block)
            if count > before[2]:
                raise ValueError('Book destination changed during scan')
            digest.update(block)
        after = os.fstat(handle.fileno())
        if (count != before[2] or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_mode, after.st_nlink) != before):
            raise ValueError('Book destination changed during scan')
    if _book_target_version(path) != before:
        raise ValueError('Book destination changed during scan')
    return digest.hexdigest()


def _cleanup_observation_stat(name, directory_fd):
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _cleanup_observation_open(name, flags, directory_fd):
    return os.open(name, flags, dir_fd=directory_fd)


def inspect_book_cleanup_workspace(workspace, *, byte_limit, entry_limit, cancel_check=None):
    """Observe workspace contents without granting deletion or recovery authority.

    All reads are descriptor-relative and refuse symlinked ancestors. The
    observation is not an atomic snapshot, authentication, or permission to
    remove files. Unsupported platforms fail closed until qualified.
    """
    from book_recovery_archive import MAX_FILES, _relative_name, _validate_portable_inventory_names
    required = (os.open, os.stat, os.readlink)
    if (any(function not in os.supports_dir_fd for function in required)
            or os.scandir not in os.supports_fd
            or os.stat not in os.supports_follow_symlinks
            or not all(getattr(os, name, 0) for name in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK'))):
        raise ValueError('Anchored cleanup observation is unavailable on this platform')
    if (type(byte_limit) is not int or not 1 <= byte_limit < 2**63
            or type(entry_limit) is not int or not 1 <= entry_limit <= MAX_FILES):
        raise ValueError('Cleanup observation requires bounded positive limits')
    root = Path(os.path.abspath(workspace))
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root)):
        raise ValueError('Cleanup observation requires a direct workspace')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            _check_stream_cancellation(cancel_check)
            next_fd = _cleanup_observation_open(component, flags, root_fd)
            os.close(root_fd)
            root_fd = next_fd
    except BaseException:
        os.close(root_fd)
        raise
    budget = _BookScanBudget(byte_limit, entry_limit, cancel_check)
    rows, metadata_size = [], [0]
    def version(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_nlink)
    def open_parts(parts):
        current = os.dup(root_fd)
        try:
            for component in parts:
                _check_stream_cancellation(cancel_check)
                child = _cleanup_observation_open(component, flags, current)
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise
    def append_row(row):
        # Account for metadata during collection, not only after all entries
        # have already been retained in memory.
        metadata_size[0] += len(json.dumps(row, ensure_ascii=True, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode('utf-8')) + 1
        if metadata_size[0] > MAX_BOOK_JOURNAL_BYTES:
            raise ValueError('Cleanup observation exceeds its metadata limit')
        rows.append(row)
    def scan(root_version):
        pending = [((), root_version)]
        while pending:
            _check_stream_cancellation(cancel_check)
            parts, expected = pending.pop()
            directory_fd = open_parts(parts)
            try:
                before = version(os.fstat(directory_fd))
                if not stat.S_ISDIR(before[5]) or before != expected:
                    raise ValueError('Cleanup directory changed during observation')
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        budget.entry()
                        name_parts = (*parts, entry.name)
                        try:
                            name = _relative_name('/'.join(name_parts))
                        except UnicodeEncodeError as error:
                            raise ValueError('Cleanup workspace contains an unportable name') from error
                        initial = version(_cleanup_observation_stat(entry.name, directory_fd))
                        row = {'path':name, 'file_version':list(initial)}
                        if stat.S_ISDIR(initial[5]):
                            row['kind'] = 'directory'
                            append_row(row)
                            pending.append((name_parts, initial))
                        elif stat.S_ISREG(initial[5]):
                            budget.file(initial[2])
                            file_fd = _cleanup_observation_open(entry.name,
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, directory_fd)
                            digest, size = hashlib.sha256(), 0
                            try:
                                stream = os.fdopen(file_fd, 'rb')
                            except BaseException:
                                os.close(file_fd)
                                raise
                            with stream:
                                if version(os.fstat(stream.fileno())) != initial:
                                    raise ValueError('Cleanup file changed during observation')
                                while True:
                                    _check_stream_cancellation(cancel_check)
                                    block = stream.read(min(1024 * 1024, initial[2] - size + 1))
                                    if not block:
                                        break
                                    size += len(block)
                                    if size > initial[2]:
                                        raise ValueError('Cleanup file changed during observation')
                                    digest.update(block)
                                if size != initial[2] or version(os.fstat(stream.fileno())) != initial:
                                    raise ValueError('Cleanup file changed during observation')
                            row.update(kind='file', sha256=digest.hexdigest())
                            append_row(row)
                        elif stat.S_ISLNK(initial[5]):
                            budget.file(initial[2])
                            target = os.readlink(entry.name, dir_fd=directory_fd)
                            if len(os.fsencode(target)) != initial[2]:
                                raise ValueError('Cleanup symlink changed during observation')
                            row.update(kind='symlink', target=target)
                            append_row(row)
                        else:
                            raise ValueError('Cleanup workspace contains an unsupported object')
                        if version(_cleanup_observation_stat(entry.name, directory_fd)) != initial:
                            raise ValueError('Cleanup workspace changed during observation')
                if version(os.fstat(directory_fd)) != before:
                    raise ValueError('Cleanup directory changed during observation')
            finally:
                os.close(directory_fd)
    try:
        root_version = version(os.fstat(root_fd))
        if not stat.S_ISDIR(root_version[5]) or version(root.lstat()) != root_version:
            raise ValueError('Cleanup workspace changed during observation')
        scan(root_version)
        for row in rows:
            _check_stream_cancellation(cancel_check)
            parts = row['path'].split('/')
            parent = open_parts(parts[:-1])
            try:
                if list(version(_cleanup_observation_stat(parts[-1], parent))) != row['file_version']:
                    raise ValueError('Cleanup workspace changed during observation')
                if row['kind'] == 'symlink' and os.readlink(parts[-1], dir_fd=parent) != row['target']:
                    raise ValueError('Cleanup symlink changed during observation')
            finally:
                os.close(parent)
        if version(os.fstat(root_fd)) != root_version or version(root.lstat()) != root_version:
            raise ValueError('Cleanup workspace changed during observation')
        _validate_portable_inventory_names([row['path'] for row in rows if row['kind'] != 'directory'],
                                          [row['path'] for row in rows if row['kind'] == 'directory'],
                                          cancel_check)
        document = {'version':1, 'workspace_identity':list(root_version[:2]),
                    'entries':sorted(rows, key=lambda row: row['path']), 'bytes':budget.bytes}
        encoded = bytearray()
        for part in json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).iterencode(document):
            _check_stream_cancellation(cancel_check)
            encoded.extend(part.encode('utf-8'))
            if len(encoded) > MAX_BOOK_JOURNAL_BYTES:
                raise ValueError('Cleanup observation exceeds its metadata limit')
        return {**document, 'sha256':hashlib.sha256(encoded).hexdigest()}
    finally:
        os.close(root_fd)


def decode_book_cleanup_observation(raw, expected_sha256, *, byte_limit, entry_limit, cancel_check=None):
    """Validate retained observation bytes; this never grants deletion authority."""
    from book_recovery_archive import MAX_FILES, _relative_name, _validate_portable_inventory_names
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_BOOK_JOURNAL_BYTES
            or not _valid_book_digest(expected_sha256)
            or type(byte_limit) is not int or not 1 <= byte_limit < 2**63
            or type(entry_limit) is not int or not 1 <= entry_limit <= MAX_FILES):
        raise ValueError('Invalid cleanup observation bounds or identity')
    _check_stream_cancellation(cancel_check)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Cleanup observation differs from its expected identity')
    # Reject pathological JSON structure before json.loads allocates an object
    # for every token. Strings are skipped without interpreting their contents;
    # json.loads still performs full syntax and escape validation afterward.
    tokens, depth, in_string, escaped = 0, 0, False, False
    for index, byte in enumerate(raw):
        if index % 65536 == 0:
            _check_stream_cancellation(cancel_check)
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
        elif byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            tokens += 1
            if depth > 8:
                raise ValueError('Cleanup observation structure exceeds its depth limit')
        elif byte in (93, 125):
            depth -= 1
            tokens += 1
            if depth < 0:
                raise ValueError('Invalid cleanup observation structure')
        elif byte in (44, 58):
            tokens += 1
        if tokens > 64 * entry_limit + 128:
            raise ValueError('Cleanup observation structure exceeds its entry limit')
    if in_string or depth != 0:
        raise ValueError('Invalid cleanup observation structure')
    def bounded_integer(value):
        if len(value) > 40:
            raise ValueError('Cleanup observation integer is too large')
        return int(value)
    try:
        document = json.loads(raw, object_pairs_hook=_journal_pairs,
            parse_int=bounded_integer,
            parse_float=lambda _: (_ for _ in ()).throw(ValueError('Invalid observation number')),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid observation number')))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValueError('Invalid cleanup observation JSON') from error
    if (type(document) is not dict or set(document) != {'version', 'workspace_identity', 'entries', 'bytes'}
            or type(document['version']) is not int or document['version'] != 1
            or type(document['workspace_identity']) is not list or len(document['workspace_identity']) != 2
            or any(type(number) is not int or not 0 <= number < 2**128 for number in document['workspace_identity'])
            or type(document['entries']) is not list or len(document['entries']) > entry_limit
            or type(document['bytes']) is not int or not 0 <= document['bytes'] <= byte_limit):
        raise ValueError('Invalid cleanup observation structure')
    names, directories, files, total = set(), set(), [], 0
    for row in document['entries']:
        _check_stream_cancellation(cancel_check)
        if (type(row) is not dict or len(row) not in (3, 4) or type(row.get('kind')) is not str
                or row['kind'] not in ('directory', 'file', 'symlink')):
            raise ValueError('Invalid cleanup observation entry')
        kind = row['kind']
        fields = {'path', 'file_version', 'kind'} | ({'sha256'} if kind == 'file' else
                                                      {'target'} if kind == 'symlink' else set())
        if (set(row) != fields or type(row['file_version']) is not list or len(row['file_version']) != 7
                or any(type(number) is not int for number in row['file_version'])
                or any(abs(number) >= 2**128 for number in row['file_version'])
                or any(row['file_version'][index] < 0 for index in (0, 1, 2, 5))
                or row['file_version'][6] < 1):
            raise ValueError('Invalid cleanup observation entry')
        try:
            name = _relative_name(row['path'])
        except (UnicodeError, TypeError) as error:
            raise ValueError('Invalid cleanup observation path') from error
        if name in names:
            raise ValueError('Duplicate cleanup observation path')
        names.add(name)
        mode = row['file_version'][5]
        if kind == 'directory':
            if not stat.S_ISDIR(mode):
                raise ValueError('Invalid cleanup observation directory')
            directories.add(name)
        elif kind == 'file':
            if not stat.S_ISREG(mode) or not _valid_book_digest(row['sha256']):
                raise ValueError('Invalid cleanup observation file')
            total += row['file_version'][2]
            files.append(name)
        else:
            if not stat.S_ISLNK(mode) or type(row['target']) is not str or '\0' in row['target']:
                raise ValueError('Invalid cleanup observation symlink')
            try:
                target_size = len(os.fsencode(row['target']))
            except UnicodeError as error:
                raise ValueError('Invalid cleanup observation symlink') from error
            if target_size != row['file_version'][2]:
                raise ValueError('Cleanup observation symlink size differs')
            total += target_size
            files.append(name)
        if total > byte_limit:
            raise ValueError('Cleanup observation exceeds its byte limit')
    if total != document['bytes']:
        raise ValueError('Cleanup observation byte count differs')
    for name in names:
        _check_stream_cancellation(cancel_check)
        parts = name.split('/')
        if any('/'.join(parts[:index]) not in directories for index in range(1, len(parts))):
            raise ValueError('Cleanup observation is missing a parent directory')
    _validate_portable_inventory_names(files, directories, cancel_check)
    _check_stream_cancellation(cancel_check)
    return document


def encode_book_cleanup_observation(observation, *, byte_limit, entry_limit, cancel_check=None):
    """Export observation bytes after validation; they remain non-authoritative."""
    from book_recovery_archive import MAX_FILES
    if type(observation) is not dict or set(observation) != {
            'version', 'workspace_identity', 'entries', 'bytes', 'sha256'}:
        raise ValueError('Invalid cleanup observation structure')
    if (type(byte_limit) is not int or not 1 <= byte_limit < 2**63
            or type(entry_limit) is not int or not 1 <= entry_limit <= MAX_FILES
            or type(observation['version']) is not int or observation['version'] != 1
            or type(observation['workspace_identity']) is not list
            or len(observation['workspace_identity']) != 2
            or any(type(n) is not int or not 0 <= n < 2**128 for n in observation['workspace_identity'])
            or type(observation['bytes']) is not int or not 0 <= observation['bytes'] <= byte_limit
            or type(observation['entries']) is not list or len(observation['entries']) > entry_limit
            or not _valid_book_digest(observation['sha256'])):
        raise ValueError('Invalid cleanup observation structure')
    def bounded_string(value):
        if type(value) is not str or len(value) > MAX_BOOK_JOURNAL_BYTES:
            raise ValueError('Cleanup observation exceeds its metadata limit')
        size = 2
        # Bound escaped size without asking the JSON encoder to allocate an
        # unbounded representation of a single caller-supplied string.
        for offset in range(0, len(value), 4096):
            _check_stream_cancellation(cancel_check)
            size += len(json.encoder.encode_basestring_ascii(value[offset:offset + 4096])) - 2
            if size > MAX_BOOK_JOURNAL_BYTES:
                raise ValueError('Cleanup observation exceeds its metadata limit')
    for row in observation['entries']:
        _check_stream_cancellation(cancel_check)
        if (type(row) is not dict or len(row) not in (3, 4) or type(row.get('kind')) is not str
                or row['kind'] not in ('directory', 'file', 'symlink')):
            raise ValueError('Invalid cleanup observation entry')
        kind = row['kind']
        fields = {'path', 'file_version', 'kind'} | ({'sha256'} if kind == 'file' else
                                                      {'target'} if kind == 'symlink' else set())
        if (set(row) != fields or type(row['file_version']) is not list or len(row['file_version']) != 7
                or any(type(n) is not int or not -(2**128) < n < 2**128 for n in row['file_version'])):
            raise ValueError('Invalid cleanup observation entry')
        bounded_string(row['path'])
        if kind == 'symlink':
            bounded_string(row['target'])
        elif kind == 'file' and not _valid_book_digest(row['sha256']):
            raise ValueError('Invalid cleanup observation entry')
    document = {key: value for key, value in observation.items() if key != 'sha256'}
    raw = bytearray()
    try:
        for part in json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).iterencode(document):
            _check_stream_cancellation(cancel_check)
            if len(part) > MAX_BOOK_JOURNAL_BYTES - len(raw):
                raise ValueError('Cleanup observation exceeds its metadata limit')
            raw.extend(part.encode('utf-8'))
    except (TypeError, RecursionError, UnicodeError) as error:
        raise ValueError('Invalid cleanup observation structure') from error
    encoded = bytes(raw)
    decode_book_cleanup_observation(encoded, observation['sha256'], byte_limit=byte_limit,
                                    entry_limit=entry_limit, cancel_check=cancel_check)
    return encoded


def compare_book_cleanup_observations(expected_raw, expected_sha256, current_raw, current_sha256,
                                      *, byte_limit, entry_limit, cancel_check=None):
    """Compare bounded observations after an interruption, without deletion authority.

    This reports differences, not ownership. The expected bytes must still be
    tied to a separately authenticated transaction before any destructive use.
    Directory timestamps and sizes may change as children are removed, so only
    their device, inode and mode are compared. A matching directory says nothing
    about its descendants or ACLs; an absent path may have been moved. Neither
    result is permission to delete anything. This is not an atomic snapshot.
    """
    expected = decode_book_cleanup_observation(expected_raw, expected_sha256,
        byte_limit=byte_limit, entry_limit=entry_limit, cancel_check=cancel_check)
    current = decode_book_cleanup_observation(current_raw, current_sha256,
        byte_limit=byte_limit, entry_limit=entry_limit, cancel_check=cancel_check)
    if expected['workspace_identity'] != current['workspace_identity']:
        raise ValueError('Cleanup workspace identity differs')
    old = {row['path']: row for row in expected['entries']}
    now = {row['path']: row for row in current['entries']}
    result = {'matching': [], 'absent': [], 'changed': [], 'unexpected': []}
    for name in sorted(old.keys() | now.keys()):
        _check_stream_cancellation(cancel_check)
        first, second = old.get(name), now.get(name)
        if first is None:
            result['unexpected'].append(name)
        elif second is None:
            result['absent'].append(name)
        elif first['kind'] != second['kind']:
            result['changed'].append(name)
        elif first['kind'] == 'directory':
            # A directory's size, timestamps and link count can change as
            # children are deleted. Its identity and mode must stay fixed.
            indexes = (0, 1, 5)
            if any(first['file_version'][i] != second['file_version'][i] for i in indexes):
                result['changed'].append(name)
            else:
                result['matching'].append(name)
        elif first != second:
            result['changed'].append(name)
        else:
            result['matching'].append(name)
    _check_stream_cancellation(cancel_check)
    return result


def _bounded_book_digest(path, budget):
    """Match the native book digest while bounding and fencing every scan."""
    budget.entry()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError('Book artifact must not be a symlink')
    if stat.S_ISREG(info.st_mode):
        return _bounded_book_file_digest(path, budget)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Book artifact must be a regular file or directory')
    rows, versions, pending = [], {}, [path]
    while pending:
        _check_stream_cancellation(budget.cancel_check)
        directory = pending.pop()
        versions[directory] = _book_target_version(directory)
        if not stat.S_ISDIR(versions[directory][5]):
            raise ValueError('Book destination directory changed during scan')
        rows.append((os.path.relpath(directory, path), 'directory', stat.S_IMODE(directory.lstat().st_mode)))
        dirs, files = [], []
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.entry()
                entry_path = Path(entry.path)
                entry_version = _book_target_version(entry_path)
                versions[entry_path] = entry_version
                is_directory = stat.S_ISDIR(entry_version[5])
                if stat.S_ISLNK(entry_version[5]):
                    # os.walk treats an unreadable or looping link as a file.
                    try:
                        is_directory = entry.is_dir()
                    except OSError:
                        is_directory = False
                if is_directory:
                    dirs.append((entry.name, entry_path, entry_version))
                else:
                    files.append((entry.name, entry_path, entry_version))
        dirs.sort(key=lambda item: item[0]); files.sort(key=lambda item: item[0])
        for _name, child, version in dirs + files:
            _check_stream_cancellation(budget.cancel_check)
            relative = os.path.relpath(child, path)
            if stat.S_ISLNK(version[5]):
                rows.append((relative, 'link', os.readlink(child)))
            elif stat.S_ISREG(version[5]):
                rows.append((relative, 'file', stat.S_IMODE(version[5]),
                             _bounded_book_file_digest(child, budget)))
            elif not stat.S_ISDIR(version[5]):
                raise ValueError('Unsupported book destination artifact')
        pending.extend(child for _, child, version in reversed(dirs) if stat.S_ISDIR(version[5]))
    for candidate, version in versions.items():
        _check_stream_cancellation(budget.cancel_check)
        if _book_target_version(candidate) != version:
            raise ValueError('Book destination changed during scan')
    # POSIX paths may contain surrogate-escaped bytes. ASCII JSON preserves a
    # deterministic representation without attempting to encode surrogates as
    # UTF-8, which would leak a raw UnicodeEncodeError out of admission.
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode('utf-8')).hexdigest()


def _validate_stream_target(row, target, old_version, budget):
    if target.is_symlink() or target.exists() != row['existed']:
        raise ValueError('Book artifact changed during stream staging')
    if row['existed']:
        if (_book_target_version(target) != old_version
                or _bounded_book_digest(target, budget) != row['old_sha256']
                or _book_target_version(target) != old_version):
            raise ValueError('Book artifact changed during stream staging')


def _validate_moved_stream_original(row, path, old_version, budget):
    # Renaming may change ctime. The remaining identity, mode and content must
    # still match the admitted original before this backup can be discarded.
    moved_version = _book_target_version(path)
    stable_fields = (0, 1, 2, 3, 5, 6)
    if (any(moved_version[index] != old_version[index] for index in stable_fields)
            or _bounded_book_digest(path, budget) != row['old_sha256']
            or _book_target_version(path) != moved_version):
        raise ValueError('Book artifact changed during its move; recovery evidence was preserved')


def _validate_moved_byte_original(row, path, old_version):
    """Detect content or metadata changes to a byte-transaction backup."""
    version = _book_target_version(path)
    stable_fields = (0, 1, 2, 3, 5, 6)  # A rename may change ctime.
    if (any(version[index] != old_version[index] for index in stable_fields)
            or _get_digest(path) != row['old_sha256']
            or _book_target_version(path) != version):
        raise ValueError('Book original changed during publication; recovery evidence was preserved')


def _validate_moved_byte_replacement(row, path):
    """Check a byte replacement after moving it aside during rollback."""
    version = _book_target_version(path)
    expected = row.get('new_file_identity')
    if (not stat.S_ISREG(version[5]) or version[6] != 1
            or not isinstance(expected, list) or len(expected) != 2
            or [version[0], version[1]] != expected
            or not _valid_book_digest(row.get('new_sha256'))
            or _get_digest(path) != row['new_sha256']
            or _book_target_version(path) != version):
        raise ValueError('Book replacement changed during recovery; recovery evidence was preserved')


def _validate_byte_original(row, path):
    expected = row.get('old_file_identity')
    version = _book_target_version(path)
    stable_fields = (0, 1, 2, 3, 5, 6)
    if (not isinstance(expected, list) or len(expected) != 6
            or [version[index] for index in stable_fields] != expected
            or _get_digest(path) != row.get('old_sha256')
            or _book_target_version(path) != version):
        raise ValueError('Active-book original artifact is missing or changed')


def _validate_byte_replacement(row, path):
    expected = row.get('new_file_identity')
    version = _book_target_version(path)
    if (not isinstance(expected, list) or len(expected) != 2
            or [version[0], version[1]] != expected
            or not stat.S_ISREG(version[5]) or version[6] != 1
            or _get_digest(path) != row.get('new_sha256')
            or _book_target_version(path) != version):
        raise ValueError('Active-book replacement changed; refusing destructive recovery')


def _validate_staged_replacement(row, path, budget=None):
    if row.get('new_sha256') is None:
        if os.path.lexists(path):
            raise ValueError('Unexpected staged replacement; publication was refused')
        return
    if not os.path.lexists(path):
        raise ValueError('Staged book replacement is missing; publication was refused')
    if budget is None:
        _validate_byte_replacement(row, path)
    else:
        _validate_stream_replacement(row, path, budget)


def _validate_stream_replacement(row, path, budget):
    expected = row.get('new_file_identity')
    if (not isinstance(expected, list) or len(expected) != 2
            or any(type(value) is not int or value < 0 for value in expected)):
        raise ValueError('Invalid active-book replacement identity')
    version = _book_target_version(path)
    if (not stat.S_ISREG(version[5]) or version[6] != 1
            or [version[0], version[1]] != expected):
        raise ValueError('Active-book replacement ownership changed; refusing destructive recovery')
    if (row.get('new_sha256') is None
            or _bounded_book_digest(path, budget) != row['new_sha256']
            or _book_target_version(path) != version):
        raise ValueError('Active-book replacement changed; refusing destructive recovery')


def _validate_stream_original(row, path, budget):
    expected = row.get('old_file_identity')
    if (not isinstance(expected, list) or len(expected) != 6
            or any(type(value) is not int for value in expected)
            or any(expected[index] < 0 for index in (0, 1, 2, 4, 5))):
        raise ValueError('Invalid active-book original identity')
    version = _book_target_version(path)
    stable_fields = (0, 1, 2, 3, 5, 6)  # ctime changes when the original is renamed.
    if ([version[index] for index in stable_fields] != expected
            or _bounded_book_digest(path, budget) != row.get('old_sha256')
            or _book_target_version(path) != version):
        raise ValueError('Active-book original artifact is missing or changed')


def _valid_book_digest(value):
    return type(value) is str and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def _verify_committed_stream_targets(rows, targets, budget):
    for row, target in zip(rows, targets):
        _check_stream_cancellation(budget.cancel_check)
        if row.get('new_sha256') is None:
            if os.path.lexists(target):
                raise ValueError('Committed book removal changed; recovery evidence was preserved')
        elif not target.exists():
            raise ValueError('Committed book replacement is missing; recovery evidence was preserved')
        else:
            _validate_stream_replacement(row, target, budget)


def _verify_stream_cleanup_targets(root, data):
    rows = data['entries']
    targets = [_get_path(root, row['path']) for row in rows]
    limits = data['stream_limits']
    budget = _BookScanBudget(limits['bytes'], limits['entries'], None)
    if data['phase'] == 'committed':
        _verify_committed_stream_targets(rows, targets, budget)
        return
    if data['phase'] == 'publishing':
        for row, target in zip(rows, targets):
            try:
                if row['existed']:
                    if not os.path.lexists(target):
                        raise ValueError('missing original')
                    _validate_stream_original(row, target, budget)
                elif os.path.lexists(target):
                    raise ValueError('occupied target')
            except ValueError as error:
                raise ValueError('Book artifact changed during stream staging; cleanup was refused') from error
        return
    if data['phase'] != 'recovered':
        raise ValueError('Book transaction is not ready for cleanup')
    for row, target in zip(rows, targets):
        if row['existed']:
            if not os.path.lexists(target):
                raise ValueError('Recovered book original changed; cleanup was refused')
            _validate_stream_original(row, target, budget)
        elif os.path.lexists(target):
            raise ValueError('Recovered book target changed; cleanup was refused')


def is_book_state_admitted(data_dir):
    try:
        root_path, identity = _direct_book_root(data_dir)
    except (OSError, ValueError):
        return False
    root = os.path.normcase(str(root_path))
    owner = _get_book_scope_owner()
    return any(scope.active and scope.root == root and scope.owner == owner
               and scope.identity == identity
               for scope in _held_books.get())


@contextlib.contextmanager
def reuse_book_state(data_dir, timeout=10):
    """Reuse only this live task/thread/process's actual kernel admission."""
    if is_book_state_admitted(data_dir):
        if os.path.lexists(Path(data_dir) / JOURNAL):
            raise ValueError('Book transaction must finish before reusing its state.')
        yield
    else:
        with ensure_book_state(data_dir, timeout=timeout):
            yield


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


def _journal_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate active-book transaction field')
        result[key] = value
    return result


def _save_journal(root, data, *, expected_version=None, create=False, root_fd=None):
    payload = bytearray()
    for part in json.JSONEncoder(allow_nan=False).iterencode(data):
        raw = part.encode('utf-8')
        if len(payload) + len(raw) > MAX_BOOK_JOURNAL_BYTES:
            raise ValueError('Active-book transaction exceeds its journal limit')
        payload.extend(raw)
    path = root / JOURNAL
    def observed_version():
        if root_fd is None:
            return _book_target_version(path)
        info = os.stat(JOURNAL, dir_fd=root_fd, follow_symlinks=False)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_nlink)
    if create:
        temporary = f'{JOURNAL}.new-{uuid.uuid4().hex}'
        temporary_path = root / temporary
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = None
        created_version = None
        completed = False
        try:
            descriptor = os.open(temporary if root_fd is not None else temporary_path, flags, 0o600,
                                 dir_fd=root_fd)
            info = os.fstat(descriptor)
            created_version = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                               info.st_ctime_ns, info.st_mode, info.st_nlink)
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = None
                if output.write(payload) != len(payload):
                    raise OSError('Short active-book journal write')
                output.flush(); os.fsync(output.fileno())
            from source_upload_publication import move_source_upload_exclusively
            root_info = root.lstat()
            if root_fd is not None:
                opened_root = os.fstat(root_fd)
                if ((opened_root.st_dev, opened_root.st_ino) !=
                        (root_info.st_dev, root_info.st_ino)):
                    raise ValueError('Book root changed before journal publication')
                move_source_upload_exclusively(
                    temporary, JOURNAL, source_dir_fd=root_fd, destination_dir_fd=root_fd)
                os.fsync(root_fd)
            else:
                move_source_upload_exclusively(temporary_path, path)
                sync_adapter_directory(str(root))
            result = observed_version()
            if (result[:2] != created_version[:2] or result[2] != len(payload)
                    or not stat.S_ISREG(result[5]) or result[6] != 1):
                raise ValueError('Book transaction journal changed during publication')
            completed = True
            return result
        except OSError as error:
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                error = ValueError('Book transaction journal is already occupied')
            raise error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            # The final name appears only after a complete, durable temporary
            # file is atomically published. On publication failure, remove only
            # this call's exact inode from either possible location.
            removed = False
            for name, candidate in (() if completed else
                                    ((temporary, temporary_path), (JOURNAL, path))):
                try:
                    current = (_book_target_version(candidate) if root_fd is None else
                               (lambda info: (info.st_dev, info.st_ino, info.st_size,
                                  info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
                                  info.st_nlink))(os.stat(name, dir_fd=root_fd,
                                                         follow_symlinks=False)))
                    if (created_version is not None and current[:2] == created_version[:2]
                            and stat.S_ISREG(current[5]) and current[6] == 1):
                        if root_fd is not None:
                            os.unlink(name, dir_fd=root_fd)
                        else:
                            candidate.unlink()
                        removed = True
                except FileNotFoundError:
                    pass
            if removed:
                if root_fd is not None:
                    os.fsync(root_fd)
                else:
                    sync_adapter_directory(str(root))
    if expected_version is not None and observed_version() != expected_version:
        raise ValueError('Book transaction journal changed; update was refused')
    if root_fd is not None:
        temporary = f'{JOURNAL}.new-{uuid.uuid4().hex}'
        descriptor = None
        created_version = None
        try:
            descriptor = os.open(temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                0o600, dir_fd=root_fd)
            created_version = _book_info_version(os.fstat(descriptor))
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = None
                if output.write(payload) != len(payload):
                    raise OSError('Short active-book journal write')
                output.flush(); os.fsync(output.fileno())
            if observed_version() != expected_version:
                raise ValueError('Book transaction journal changed; update was refused')
            os.replace(temporary, JOURNAL, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            return observed_version()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                current = os.stat(temporary, dir_fd=root_fd, follow_symlinks=False)
                version = _book_info_version(current)
                if (created_version is not None and version[:2] == created_version[:2]
                        and stat.S_ISREG(version[5]) and version[6] == 1):
                    os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
    save_adapter_publication_bytes(str(path), bytes(payload))
    return _book_target_version(path)


def _read_book_journal(path):
    before = _book_target_version(path)
    if not stat.S_ISREG(before[5]) or before[6] != 1 or before[2] > MAX_BOOK_JOURNAL_BYTES:
        raise ValueError('Active-book journal must be a bounded regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                opened.st_ctime_ns, opened.st_mode, opened.st_nlink) != before:
            raise ValueError('Active-book journal changed while opening')
        raw = stream.read(before[2] + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) != before[2] or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_mode, after.st_nlink) != before):
            raise ValueError('Active-book journal changed while reading')
    if _book_target_version(path) != before:
        raise ValueError('Active-book journal changed while reading')
    try:
        return (json.loads(raw, object_pairs_hook=_journal_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid journal number'))),
                before)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid active-book transaction') from error


def _move(source, destination, *, exclusive=False, anchor=None, anchor_identity=None,
          root_guard=None):
    if exclusive:
        from source_upload_publication import move_source_upload_exclusively
        if anchor is None:
            move_source_upload_exclusively(source, destination)
        elif os.name == 'nt':
            from windows_export_staging_directory import guard_windows_book_move_parents
            with guard_windows_book_move_parents(
                    anchor, source, destination, root_guard=root_guard):
                current_root = Path(anchor).lstat()
                if (anchor_identity is not None and
                        (current_root.st_dev, current_root.st_ino) != anchor_identity):
                    raise ValueError('Book move root changed; recovery evidence was preserved')
                move_source_upload_exclusively(source, destination)
            return
        else:
            if (os.open not in os.supports_dir_fd
                    or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')):
                raise OSError(errno.ENOTSUP, 'Safe anchored book moves are unavailable')
            root = Path(os.path.abspath(anchor))
            if (root.is_symlink() or os.path.normcase(os.path.realpath(root)) !=
                    os.path.normcase(str(root))):
                raise ValueError('Book move root changed; recovery evidence was preserved')
            source, destination = Path(os.path.abspath(source)), Path(os.path.abspath(destination))
            try:
                source_rel = source.relative_to(root)
                destination_rel = destination.relative_to(root)
            except ValueError as error:
                raise ValueError('Book move escapes its admitted root') from error
            if not source_rel.parts or not destination_rel.parts:
                raise ValueError('Book move requires file or directory names')
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            with contextlib.ExitStack() as stack:
                root_fd = os.open(root, flags)
                stack.callback(os.close, root_fd)
                opened_root, named_root = os.fstat(root_fd), root.lstat()
                if ((opened_root.st_dev, opened_root.st_ino) !=
                        (named_root.st_dev, named_root.st_ino)
                        or anchor_identity is not None
                        and (opened_root.st_dev, opened_root.st_ino) != anchor_identity):
                    raise ValueError('Book move root changed; recovery evidence was preserved')
                def parent_fd(relative):
                    # Retain only the current descriptor while walking. A
                    # deeply nested book must not exhaust the process limit.
                    current = os.dup(root_fd)
                    try:
                        for component in relative.parts[:-1]:
                            next_fd = os.open(component, flags, dir_fd=current)
                            os.close(current)
                            current = next_fd
                    except BaseException:
                        os.close(current)
                        raise
                    stack.callback(os.close, current)
                    return current
                source_fd = parent_fd(source_rel)
                destination_fd = parent_fd(destination_rel)
                def require_named_parents():
                    for relative, opened in ((source_rel, source_fd), (destination_rel, destination_fd)):
                        current = parent_fd(relative)
                        first, second = os.fstat(opened), os.fstat(current)
                        if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
                            raise ValueError('Book move parent changed; recovery evidence was preserved')
                require_named_parents()
                move_source_upload_exclusively(source_rel.name, destination_rel.name,
                    source_dir_fd=source_fd, destination_dir_fd=destination_fd)
                try:
                    require_named_parents()
                except BaseException as parent_error:
                    # A parent renamed after the pre-move check still leaves the
                    # descriptor valid, but the destination is no longer inside
                    # the admitted root. Move our exact entry back to its pinned
                    # source directory before reporting the relocation.
                    try:
                        move_source_upload_exclusively(destination_rel.name,
                            source_rel.name, source_dir_fd=destination_fd,
                            destination_dir_fd=source_fd)
                        os.fsync(source_fd)
                        if source_fd != destination_fd:
                            os.fsync(destination_fd)
                    except BaseException as rollback_error:
                        raise ValueError(
                            'Book move parent changed and compensation failed; '
                            'recovery evidence was preserved') from rollback_error
                    raise parent_error
                os.fsync(source_fd)
                if source_fd != destination_fd:
                    os.fsync(destination_fd)
            return
    else:
        os.replace(source, destination)
    sync_adapter_directory(str(source.parent))
    if destination.parent != source.parent:
        sync_adapter_directory(str(destination.parent))


def _capture_cleanup_tree(path):
    """Capture exact descendant identities through a no-follow root handle."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(path, flags)
    rows, count = {}, [0]
    def version(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_nlink)
    def scan(directory_fd, prefix):
        if len(prefix) > 128:
            raise ValueError('Book cleanup directory depth exceeds its limit')
        before = version(os.fstat(directory_fd))
        children = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                count[0] += 1
                if count[0] > MAX_LEGACY_CLEANUP_ENTRIES:
                    raise ValueError('Book cleanup entry count exceeds its limit')
                info = entry.stat(follow_symlinks=False)
                current = version(info)
                if (not stat.S_ISDIR(info.st_mode)
                        and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                    raise ValueError('Book cleanup tree contains an unsafe artifact')
                relative = (*prefix, entry.name)
                rows['/'.join(relative)] = current
                children.append((entry.name, relative, current))
        for name, relative, current in children:
            if stat.S_ISDIR(current[5]):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    if version(os.fstat(child_fd)) != current:
                        raise ValueError('Book cleanup tree changed during validation')
                    scan(child_fd, relative)
                finally:
                    os.close(child_fd)
            current_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if version(current_info) != current:
                raise ValueError('Book cleanup tree changed during validation')
        if version(os.fstat(directory_fd)) != before:
            raise ValueError('Book cleanup tree changed during validation')
    try:
        scan(root_fd, ())
    finally:
        os.close(root_fd)
    return rows


def _capture_cleanup_tree_path(path, depth=0, prefix=(), rows=None):
    """Capture a Windows cleanup inventory for later native-handle revalidation."""
    if depth > 128:
        raise ValueError('Book cleanup directory depth exceeds its limit')
    rows = {} if rows is None else rows
    before_info = Path(path).lstat()
    before = _book_info_version(before_info)
    if (not stat.S_ISDIR(before[5]) or stat.S_ISLNK(before[5])
            or getattr(before_info, 'st_file_attributes', 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Book cleanup tree contains an unsafe artifact')
    with os.scandir(path) as entries:
        children = sorted((entry.name, Path(path) / entry.name) for entry in entries)
    for name, child in children:
        if len(rows) >= MAX_LEGACY_CLEANUP_ENTRIES:
            raise ValueError('Book cleanup entry count exceeds its limit')
        child_info = child.lstat()
        version = _book_info_version(child_info)
        if (getattr(child_info, 'st_file_attributes', 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISDIR(version[5])
                and not (stat.S_ISREG(version[5]) and version[6] == 1)):
            raise ValueError('Book cleanup tree contains an unsafe artifact')
        relative = (*prefix, name)
        rows['/'.join(relative)] = version
        if stat.S_ISDIR(version[5]):
            _capture_cleanup_tree_path(child, depth + 1, relative, rows)
        if _book_target_version(child) != version:
            raise ValueError('Book cleanup tree changed during validation')
    if _book_target_version(path) != before:
        raise ValueError('Book cleanup tree changed during validation')
    return rows


def _cleanup_tree_inventory(path):
    return (_capture_cleanup_tree_path(path) if os.name == 'nt'
            else _capture_cleanup_tree(path))


def _validate_stream_workspace_for_cleanup(workspace, data):
    """Refuse cleanup if the workspace contains anything not proven owned."""
    if not os.path.lexists(workspace):
        return {}
    before = _book_target_version(workspace)
    if not stat.S_ISDIR(before[5]):
        raise ValueError('Book transaction workspace changed; cleanup was refused')

    rows, phase, limits = data['entries'], data['phase'], data['stream_limits']
    allowed = {}
    for index, row in enumerate(rows):
        if phase == 'committed' and row['existed']:
            allowed[f'old-{index}'] = ('original', row)
        if phase in ('publishing', 'recovered') and row['new_sha256'] is not None:
            allowed[f'new-{index}'] = ('replacement', row)
        if phase == 'recovered' and row['new_sha256'] is not None:
            allowed[f'discard-{index}'] = ('replacement', row)
    seen = set()
    with os.scandir(workspace) as entries:
        for entry in entries:
            if entry.name not in allowed or entry.name in seen:
                raise ValueError('Book transaction workspace contains unowned data; cleanup was refused')
            seen.add(entry.name)
    for index in range(len(rows)):
        if f'new-{index}' in seen and f'discard-{index}' in seen:
            raise ValueError('Book transaction replacement has multiple owners; cleanup was refused')
    original_budget = _BookScanBudget(limits['bytes'], limits['entries'], None)
    replacement_budget = _BookScanBudget(limits['bytes'], limits['entries'], None)
    validated = {}
    for name in sorted(seen):
        kind, row = allowed[name]
        version = _book_target_version(workspace / name)
        if kind == 'original':
            _validate_stream_original(row, workspace / name, original_budget)
        else:
            _validate_stream_replacement(row, workspace / name, replacement_budget)
        if _book_target_version(workspace / name) != version:
            raise ValueError('Book transaction workspace changed; cleanup was refused')
        validated[name] = {
            'version': version,
            'tree': _cleanup_tree_inventory(workspace / name) if stat.S_ISDIR(version[5]) else None,
        }
    if _book_target_version(workspace) != before:
        raise ValueError('Book transaction workspace changed; cleanup was refused')
    return validated

def _cleanup_unpublished_stream_workspace(workspace, workspace_identity, staged_identities,
                                          *, parent_fd=None, staged_versions=None,
                                          require_stage_versions=False):
    """Remove only checked staging files after a pre-journal error.

    A completed stage can be checked against its full post-write version as
    well as its identity. This catches many reused-inode replacements; it is
    not durable ownership proof after a crash or on a coarse-clock filesystem.
    """
    if (require_stage_versions and (staged_versions is None
            or not set(staged_identities).issubset(staged_versions))):
        raise ValueError('Book staging version unavailable; cleanup was refused')
    anchored = (os.name != 'nt' and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
                and os.unlink in os.supports_dir_fd and os.rmdir in os.supports_dir_fd
                and os.scandir in os.supports_fd and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'))
    with contextlib.ExitStack() as stack:
        directory_fd = None
        if anchored:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            owned_parent_fd = (os.dup(parent_fd) if parent_fd is not None
                               else os.open(workspace.parent, flags))
            stack.callback(os.close, owned_parent_fd)
            try:
                directory_fd = os.open(workspace.name, flags, dir_fd=owned_parent_fd)
            except FileNotFoundError:
                return
            stack.callback(os.close, directory_fd)
            info = os.fstat(directory_fd)
        else:
            if not os.path.lexists(workspace):
                return
            info = workspace.lstat()
        if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != workspace_identity):
            raise ValueError('Book staging workspace changed; cleanup was refused')
        names = []
        with os.scandir(directory_fd if anchored else workspace) as entries:
            for entry in entries:
                expected = staged_identities.get(entry.name)
                current = entry.stat(follow_symlinks=False)
                if os.name == 'nt':
                    from windows_export_staging_directory import windows_named_file_identity
                    current_identity = windows_named_file_identity(workspace / entry.name)
                    # ``DirEntry.stat().st_nlink`` is documented as zero on
                    # Windows.  The native helper has already
                    # required BY_HANDLE_FILE_INFORMATION.nNumberOfLinks == 1.
                    single_link = True
                else:
                    current_identity = (current.st_dev, current.st_ino)
                    single_link = current.st_nlink == 1
                has_version = staged_versions is not None and entry.name in staged_versions
                current_version = ((_book_target_version(workspace / entry.name)
                                    if os.name == 'nt' else _book_info_version(current))
                                   if has_version else None)
                if (expected is None or not stat.S_ISREG(current.st_mode) or not single_link
                        or current_identity != expected
                        or (has_version and current_version != staged_versions[entry.name])):
                    raise ValueError(
                        'Book staging workspace contains unowned data; cleanup was refused '
                        f'(name={entry.name!r}, expected={expected!r}, '
                        f'current={current_identity!r}, mode={current.st_mode!r}, '
                        f'links={current.st_nlink!r}, '
                        f'version_changed={has_version and current_version != staged_versions[entry.name]!r})')
                names.append(entry.name)
        for name in names:
            current = (os.stat(name, dir_fd=directory_fd, follow_symlinks=False) if anchored
                       else (workspace / name).lstat())
            if os.name == 'nt':
                current_identity = windows_named_file_identity(workspace / name)
                single_link = True
            else:
                current_identity = (current.st_dev, current.st_ino)
                single_link = current.st_nlink == 1
            if (not stat.S_ISREG(current.st_mode) or not single_link
                    or current_identity != staged_identities[name]
                    or (staged_versions is not None and name in staged_versions
                        and _book_info_version(current) != staged_versions[name])):
                raise ValueError(
                    'Book staging workspace changed; cleanup was refused '
                    f'(name={name!r}, expected={staged_identities[name]!r}, '
                    f'current={current_identity!r}, mode={current.st_mode!r}, '
                    f'links={current.st_nlink!r})')
            _unlink_owned_staging_file(workspace, name, directory_fd)
        with os.scandir(directory_fd if anchored else workspace) as entries:
            if next(entries, None) is not None:
                raise ValueError('Book staging workspace contains unowned data; cleanup was refused')
        current = (os.stat(workspace.name, dir_fd=owned_parent_fd, follow_symlinks=False) if anchored
                   else workspace.lstat())
        if (not stat.S_ISDIR(current.st_mode) or
                (current.st_dev, current.st_ino) != workspace_identity):
            raise ValueError('Book staging workspace changed; cleanup was refused')
        try:
            if anchored:
                os.fsync(directory_fd)
                os.rmdir(workspace.name, dir_fd=owned_parent_fd)
                os.fsync(owned_parent_fd)
            else:
                os.rmdir(workspace)
                sync_adapter_directory(str(workspace.parent))
        except OSError as error:
            try:
                with os.scandir(directory_fd if anchored else workspace) as entries:
                    populated = next(entries, None) is not None
            except FileNotFoundError:
                populated = False
            if populated or error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise ValueError('Book staging workspace contains unowned data; cleanup was refused') from error
            raise


def _unlink_owned_staging_file(workspace, name, directory_fd):
    if directory_fd is not None:
        os.unlink(name, dir_fd=directory_fd)
    else:
        os.unlink(workspace / name)


def _validate_byte_cleanup_targets(rows, targets, phase):
    for row, target in zip(rows, targets):
        expected = row['new_sha256'] if phase == 'committed' else row['old_sha256']
        should_exist = expected is not None
        if os.path.lexists(target) != should_exist:
            raise ValueError('Book transaction target changed; cleanup was refused')
        if should_exist:
            if phase == 'committed' and 'new_file_identity' in row:
                _validate_byte_replacement(row, target)
            elif phase == 'recovered' and 'old_file_identity' in row:
                _validate_byte_original(row, target)
            elif _get_digest(target) != expected:
                raise ValueError('Book transaction target changed; cleanup was refused')


def _validate_byte_workspace_for_cleanup(workspace, data):
    """Return exact regular-file identities a legacy cleanup may remove."""
    phase, rows = data['phase'], data['entries']
    if phase not in ('committed', 'recovered'):
        raise ValueError('Book transaction is not ready for cleanup')
    allowed = {}
    for index, row in enumerate(rows):
        if phase == 'committed' and row['existed']:
            allowed[f'old-{index}'] = (row['old_sha256'], row.get('old_file_identity'))
        elif phase == 'recovered' and row['new_sha256'] is not None:
            allowed[f'discard-{index}'] = (row['new_sha256'], row.get('new_file_identity'))
            allowed[f'new-{index}'] = (row['new_sha256'], row.get('new_file_identity'))
    if not os.path.lexists(workspace):
        return {}
    info = workspace.lstat()
    if not stat.S_ISDIR(info.st_mode) or workspace.is_symlink():
        raise ValueError('Book transaction workspace changed; cleanup was refused')
    seen = {}
    with os.scandir(workspace) as entries:
        for entry in entries:
            if entry.name not in allowed or entry.name in seen:
                raise ValueError('Book transaction workspace contains unowned data; cleanup was refused')
            version = _book_target_version(workspace / entry.name)
            expected_digest, expected_identity = allowed[entry.name]
            if expected_identity is not None:
                observed_identity = ([version[0], version[1]] if len(expected_identity) == 2
                                     else [version[index] for index in (0, 1, 2, 3, 5, 6)])
                if observed_identity != expected_identity:
                    raise ValueError('Book transaction workspace ownership changed; cleanup was refused')
            if (stat.S_ISREG(version[5]) and version[6] != 1
                    or not stat.S_ISREG(version[5]) and not stat.S_ISDIR(version[5])):
                raise ValueError('Book transaction workspace contains an unsafe artifact')
            if _get_digest(workspace / entry.name) != expected_digest:
                raise ValueError('Book transaction workspace changed; cleanup was refused')
            if _book_target_version(workspace / entry.name) != version:
                raise ValueError('Book transaction workspace changed; cleanup was refused')
            seen[entry.name] = {
                'version': version,
                'tree': _cleanup_tree_inventory(workspace / entry.name)
                        if stat.S_ISDIR(version[5]) else None,
            }
    for index in range(len(rows)):
        if f'new-{index}' in seen and f'discard-{index}' in seen:
            raise ValueError('Book transaction replacement has multiple owners; cleanup was refused')
    return seen


def _remove_owned_tree_at(parent_fd, name, expected, inventory, depth=0, prefix=()):
    """Remove exactly the previously validated tree through pinned handles."""
    if depth > 128:
        raise ValueError('Legacy book cleanup directory depth exceeds its limit')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or expected is not None and (opened.st_dev, opened.st_ino) != expected):
            raise ValueError('Book transaction directory ownership changed; cleanup was refused')
        expected_children = {
            path.split('/')[-1]: version
            for path, version in inventory.items()
            if tuple(path.split('/')[:-1]) == prefix
        }
        observed = {}
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                observed[entry.name] = (info.st_dev, info.st_ino, info.st_size,
                                       info.st_mtime_ns, info.st_ctime_ns,
                                       info.st_mode, info.st_nlink)
        if observed != expected_children:
            raise ValueError('Book transaction directory changed; cleanup was refused')
        for child, child_version in sorted(expected_children.items()):
            info = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
            current_version = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                               info.st_ctime_ns, info.st_mode, info.st_nlink)
            if current_version != child_version:
                raise ValueError('Book transaction directory changed; cleanup was refused')
            if stat.S_ISDIR(info.st_mode):
                _remove_owned_tree_at(directory_fd, child, child_version[:2], inventory,
                                      depth + 1, (*prefix, child))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                os.unlink(child, dir_fd=directory_fd)
            else:
                raise ValueError('Book transaction directory contains an unsafe artifact')
            os.fsync(directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('Book transaction directory changed; cleanup was refused')
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(directory_fd)


def _require_book_journal_version(root, expected):
    if _book_target_version(root / JOURNAL) != expected:
        raise ValueError('Book transaction journal changed; cleanup was refused')


def _remove_windows_owned_tree(path, expected, inventory, api, depth=0, prefix=()):
    """Delete only an exact Windows tree while native handles deny replacement."""
    from windows_export_staging_directory import _Handle, _identity
    if depth > 128:
        raise ValueError('Book cleanup directory depth exceeds its limit')
    current = _book_target_version(path)
    if current != expected:
        raise ValueError('Book transaction workspace changed; cleanup was refused')
    handle = _Handle(api, path, delete=True)
    try:
        info = handle.information()
        # The delete handle denies new writers. Recheck the complete admitted
        # version only after that exclusion is active, not merely its identity.
        pinned_version = _book_target_version(path)
        if (pinned_version != expected
                or _identity(info) != (expected[0], expected[1])
                or info.attributes & 0x400):
            raise ValueError('Book transaction workspace ownership changed; cleanup was refused')
        if stat.S_ISDIR(expected[5]):
            if not info.attributes & 0x10:
                raise ValueError('Book transaction workspace type changed; cleanup was refused')
            expected_children = {
                name.split('/')[-1]: version for name, version in inventory.items()
                if tuple(name.split('/')[:-1]) == prefix
            }
            with os.scandir(path) as entries:
                observed = {entry.name: _book_target_version(Path(path) / entry.name)
                            for entry in entries}
            if observed != expected_children:
                raise ValueError('Book transaction directory changed; cleanup was refused')
            for name, version in sorted(expected_children.items()):
                _remove_windows_owned_tree(Path(path) / name, version, inventory, api,
                                           depth + 1, (*prefix, name))
        elif (not stat.S_ISREG(expected[5]) or expected[6] != 1
              or info.attributes & 0x10 or info.links != 1):
            raise ValueError('Book transaction workspace contains an unsafe artifact')
        handle.delete()
    finally:
        handle.close()


def _cleanup_validated_windows_workspace(root, workspace, versions,
                                         validate_targets, journal_version):
    from windows_export_staging_directory import WindowsStageDirectory
    identity = _book_target_version(workspace)[:2]
    with WindowsStageDirectory(workspace, identity) as guard:
        validate_targets()
        _require_book_journal_version(root, journal_version)
        with os.scandir(workspace) as entries:
            observed = {entry.name for entry in entries}
        if observed != set(versions):
            raise ValueError('Book transaction workspace contains unowned data; cleanup was refused')
        for name in sorted(versions):
            validate_targets()
            _require_book_journal_version(root, journal_version)
            evidence = versions[name]
            _remove_windows_owned_tree(workspace / name, evidence['version'],
                                       evidence['tree'], guard.api)
            guard.check()
        validate_targets()
        _require_book_journal_version(root, journal_version)
        with os.scandir(workspace) as entries:
            if next(entries, None) is not None:
                raise ValueError('Book transaction workspace contains unowned data; cleanup was refused')
        guard.remove_empty_directory()


def _cleanup_validated_byte_workspace(root, workspace, data, targets, journal_version):
    """Delete only exact checked legacy files through pinned directories."""
    if os.name == 'nt':
        _validate_byte_cleanup_targets(data['entries'], targets, data['phase'])
        _require_book_journal_version(root, journal_version)
        versions = _validate_byte_workspace_for_cleanup(workspace, data)
        _cleanup_validated_windows_workspace(root, workspace, versions,
            lambda: _validate_byte_cleanup_targets(data['entries'], targets, data['phase']),
            journal_version)
        return
    if (os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd
            or os.rmdir not in os.supports_dir_fd or os.scandir not in os.supports_fd
            or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')):
        raise OSError(errno.ENOTSUP, 'Safe legacy book cleanup is unavailable')
    _validate_byte_cleanup_targets(data['entries'], targets, data['phase'])
    _require_book_journal_version(root, journal_version)
    versions = _validate_byte_workspace_for_cleanup(workspace, data)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        current_root = root.lstat()
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (current_root.st_dev, current_root.st_ino):
            raise ValueError('Book root changed; cleanup was refused')
        if os.path.lexists(workspace):
            workspace_fd = os.open(workspace.name, flags, dir_fd=root_fd)
            try:
                opened_workspace = os.fstat(workspace_fd)
                current_workspace = os.stat(workspace.name, dir_fd=root_fd, follow_symlinks=False)
                if ((opened_workspace.st_dev, opened_workspace.st_ino) !=
                        (current_workspace.st_dev, current_workspace.st_ino)):
                    raise ValueError('Book transaction workspace changed; cleanup was refused')
                for name in sorted(versions):
                    _require_book_journal_version(root, journal_version)
                    _validate_byte_cleanup_targets(data['entries'], targets, data['phase'])
                    current = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
                    evidence = versions[name]
                    version = evidence['version']
                    if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                            current.st_ctime_ns, current.st_mode, current.st_nlink) != version):
                        raise ValueError('Book transaction workspace changed; cleanup was refused')
                    if stat.S_ISDIR(current.st_mode):
                        _remove_owned_tree_at(workspace_fd, name,
                                              (current.st_dev, current.st_ino),
                                              evidence['tree'])
                    else:
                        os.unlink(name, dir_fd=workspace_fd)
                    os.fsync(workspace_fd)
                _validate_byte_cleanup_targets(data['entries'], targets, data['phase'])
                _require_book_journal_version(root, journal_version)
                current_workspace = os.stat(workspace.name, dir_fd=root_fd,
                                            follow_symlinks=False)
                if ((current_workspace.st_dev, current_workspace.st_ino) !=
                        (opened_workspace.st_dev, opened_workspace.st_ino)):
                    raise ValueError('Book transaction workspace changed; cleanup was refused')
                try:
                    os.rmdir(workspace.name, dir_fd=root_fd)
                except OSError as error:
                    if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise ValueError('Book transaction workspace contains unowned data; cleanup was refused') from error
                    raise
                os.fsync(root_fd)
            finally:
                os.close(workspace_fd)
    finally:
        os.close(root_fd)


def _cleanup_validated_stream_workspace(root, workspace, data, journal_version):
    """Remove only the exact validated stream workspace through pinned fds."""
    if os.name == 'nt':
        _verify_stream_cleanup_targets(root, data)
        _require_book_journal_version(root, journal_version)
        versions = _validate_stream_workspace_for_cleanup(workspace, data)
        _cleanup_validated_windows_workspace(root, workspace, versions,
            lambda: _verify_stream_cleanup_targets(root, data), journal_version)
        return
    if (os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd
            or os.rmdir not in os.supports_dir_fd or os.scandir not in os.supports_fd
            or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')):
        raise OSError(errno.ENOTSUP, 'Safe stream book cleanup is unavailable')
    _verify_stream_cleanup_targets(root, data)
    _require_book_journal_version(root, journal_version)
    versions = _validate_stream_workspace_for_cleanup(workspace, data)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        workspace_fd = os.open(workspace.name, flags, dir_fd=root_fd)
        try:
            opened = os.fstat(workspace_fd)
            named = os.stat(workspace.name, dir_fd=root_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError('Book transaction workspace changed; cleanup was refused')
            for name in sorted(versions):
                _require_book_journal_version(root, journal_version)
                _verify_stream_cleanup_targets(root, data)
                current = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
                evidence = versions[name]
                version = evidence['version']
                if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                        current.st_ctime_ns, current.st_mode, current.st_nlink) != version):
                    raise ValueError('Book transaction workspace changed; cleanup was refused')
                if stat.S_ISDIR(current.st_mode):
                    _remove_owned_tree_at(workspace_fd, name,
                                          (current.st_dev, current.st_ino),
                                          evidence['tree'])
                elif stat.S_ISREG(current.st_mode) and current.st_nlink == 1:
                    os.unlink(name, dir_fd=workspace_fd)
                else:
                    raise ValueError('Book transaction workspace changed; cleanup was refused')
                os.fsync(workspace_fd)
            _verify_stream_cleanup_targets(root, data)
            _require_book_journal_version(root, journal_version)
            current = os.stat(workspace.name, dir_fd=root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('Book transaction workspace changed; cleanup was refused')
            try:
                os.rmdir(workspace.name, dir_fd=root_fd)
            except OSError as error:
                if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    raise ValueError('Book transaction workspace contains unowned data; cleanup was refused') from error
                raise
            os.fsync(root_fd)
        finally:
            os.close(workspace_fd)
    finally:
        os.close(root_fd)


def _unlink_owned_book_journal(root, expected, *, root_fd=None):
    path = root / JOURNAL
    if os.name != 'nt' and (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd and hasattr(os, 'O_DIRECTORY')
            and hasattr(os, 'O_NOFOLLOW')):
        descriptor = (os.dup(root_fd) if root_fd is not None else
                      os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        try:
            info = os.stat(JOURNAL, dir_fd=descriptor, follow_symlinks=False)
            observed = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                        info.st_ctime_ns, info.st_mode, info.st_nlink)
            if observed != expected or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Book transaction journal changed; cleanup was refused')
            os.unlink(JOURNAL, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    else:
        if _book_target_version(path) != expected:
            raise ValueError('Book transaction journal changed; cleanup was refused')
        path.unlink()
        sync_adapter_directory(str(root))


def _finish_transaction(root, workspace, *, journal_version, stream_data=None,
                        byte_data=None, targets=None):
    _require_book_journal_version(root, journal_version)
    if byte_data is not None and targets is not None:
        _validate_byte_cleanup_targets(byte_data['entries'], targets, byte_data['phase'])
    if os.path.lexists(workspace):
        if stream_data is not None:
            _cleanup_validated_stream_workspace(root, workspace, stream_data,
                                                journal_version)
        elif byte_data is not None and targets is not None:
            _cleanup_validated_byte_workspace(root, workspace, byte_data, targets,
                                              journal_version)
        else:
            raise ValueError('Book transaction cleanup requires ownership evidence')
        sync_adapter_directory(str(root))
    _unlink_owned_book_journal(root, journal_version)


def _validate_committed_byte_backups(workspace, rows, targets):
    """Preserve changed legacy backups or targets before committed cleanup.

    A missing backup is permitted for recovery after an interrupted cleanup.
    This detects changes but cannot exclude writers after the check.
    """
    allowed = set()
    for row, target in zip(rows, targets):
        if not isinstance(row, dict):
            raise ValueError('Invalid active-book entry')
        new_digest = row.get('new_sha256')
        if new_digest is None:
            if os.path.lexists(target):
                raise ValueError('Committed book removal changed; recovery evidence was preserved')
        elif not os.path.lexists(target):
            raise ValueError('Committed book replacement changed; recovery evidence was preserved')
        elif 'new_file_identity' in row:
            _validate_byte_replacement(row, target)
        elif _get_digest(target) != new_digest:
            raise ValueError('Committed book replacement changed; recovery evidence was preserved')
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError('Invalid active-book entry')
        if row.get('existed') is True:
            allowed.add(f'old-{index}')
    if os.path.lexists(workspace):
        if not stat.S_ISDIR(workspace.lstat().st_mode):
            raise ValueError('Committed book workspace changed; recovery evidence was preserved')
        with os.scandir(workspace) as entries:
            for entry in entries:
                if entry.name not in allowed:
                    raise ValueError('Committed book workspace contains unowned data; cleanup was refused')
    for index, row in enumerate(rows):
        backup = workspace / f'old-{index}'
        if row.get('existed') is True and os.path.lexists(backup):
            if 'old_file_identity' in row:
                _validate_byte_original(row, backup)
            elif _get_digest(backup) != row.get('old_sha256'):
                raise ValueError('Committed book backup changed; recovery evidence was preserved')


def recover_book_state_locked(data_dir, *, cancel_check=None):
    """Restore an interrupted switch; caller holds the book-operation lock."""
    root, root_identity = _direct_book_root(data_dir)
    journal = root / JOURNAL
    if not os.path.lexists(journal):
        return False
    data, journal_version = _read_book_journal(journal)
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] not in (1, 2, 3)
            or data.get('phase') not in ('publishing', 'recovered', 'committed')):
        raise ValueError('Invalid active-book transaction')
    name = data.get('workspace')
    if not isinstance(name, str) or not name.startswith('.book-switch-') or Path(name).name != name:
        raise ValueError('Invalid active-book workspace')
    workspace = _get_path(root, name)
    rows = data.get('entries')
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Invalid active-book entries')
    limits = data.get('stream_limits')
    if data['version'] == 1 and ('stream_limits' in data or
            any(isinstance(row, dict) and ('new_file_identity' in row or 'old_file_identity' in row)
                for row in rows)):
        raise ValueError('Invalid legacy active-book transaction')
    if data['version'] == 1:
        expected_keys = {'path', 'existed', 'old_sha256', 'new_sha256'}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != expected_keys
                    or type(row.get('existed')) is not bool
                    or (row['existed'] and not _valid_book_digest(row.get('old_sha256')))
                    or (not row['existed'] and row.get('old_sha256') is not None)
                    or (row.get('new_sha256') is not None
                        and not _valid_book_digest(row['new_sha256']))):
                raise ValueError('Invalid legacy active-book entry')
        raise ValueError('Legacy active-book transaction requires explicit recovery inspection')
    from book_recovery_archive import MAX_FILES, _relative_name, _validate_names
    if len(rows) > MAX_FILES:
        raise ValueError('Active-book transaction exceeds its entry limit')
    names = [_relative_name(row.get('path')) for row in rows]
    _validate_names(names)
    if any(name.split('/')[0].casefold().startswith('.book-switch-') or
           name.split('/')[0].casefold() in (JOURNAL.casefold(), (JOURNAL + '.lock').casefold())
           for name in names):
        raise ValueError('Invalid active-book transaction targets')
    parent_identities = data.get('target_parent_identities')
    if parent_identities is not None:
        _require_transaction_parent_identities(root, names, parent_identities)
    if data['version'] == 3:
        if 'stream_limits' in data:
            raise ValueError('Invalid active-book byte transaction')
        for row in rows:
            existed, old, new = row.get('existed'), row.get('old_sha256'), row.get('new_sha256')
            expected_keys = {'path', 'existed', 'old_sha256', 'new_sha256'}
            if type(existed) is not bool:
                raise ValueError('Invalid active-book byte entry')
            if existed:
                expected_keys.add('old_file_identity')
                identity = row.get('old_file_identity')
                if (not _valid_book_digest(old) or not isinstance(identity, list) or len(identity) != 6
                        or any(type(value) is not int for value in identity)
                        or any(identity[index] < 0 for index in (0, 1, 2, 4, 5))):
                    raise ValueError('Invalid active-book byte original identity')
            elif old is not None:
                raise ValueError('Invalid active-book byte original identity')
            if new is not None:
                expected_keys.add('new_file_identity')
                identity = row.get('new_file_identity')
                if (not _valid_book_digest(new) or not isinstance(identity, list) or len(identity) != 2
                        or any(type(value) is not int or value < 0 for value in identity)):
                    raise ValueError('Invalid active-book byte replacement identity')
            if set(row) != expected_keys:
                raise ValueError('Invalid active-book byte entry')
    if data['version'] == 2:
        if (not isinstance(limits, dict) or set(limits) != {'bytes', 'entries'}
                or type(limits['bytes']) is not int or not 1 <= limits['bytes'] < 2**63
                or type(limits['entries']) is not int or not 1 <= limits['entries'] <= MAX_FILES
                or len(rows) > limits['entries'] or any(not isinstance(row, dict) for row in rows)):
            raise ValueError('Invalid active-book stream scan limits')
        for row in rows:
            existed, old, new = row.get('existed'), row.get('old_sha256'), row.get('new_sha256')
            expected_keys = {'path', 'existed', 'old_sha256', 'new_sha256'}
            if type(existed) is not bool:
                raise ValueError('Invalid active-book stream entry')
            if existed:
                expected_keys.add('old_file_identity')
                identity = row.get('old_file_identity')
                if (not _valid_book_digest(old) or not isinstance(identity, list) or len(identity) != 6
                        or any(type(value) is not int for value in identity)
                        or any(identity[index] < 0 for index in (0, 1, 2, 4, 5))):
                    raise ValueError('Invalid active-book original identity')
            elif old is not None:
                raise ValueError('Invalid active-book original identity')
            if new is not None:
                expected_keys.add('new_file_identity')
                identity = row.get('new_file_identity')
                if (not _valid_book_digest(new) or not isinstance(identity, list) or len(identity) != 2
                        or any(type(value) is not int or value < 0 for value in identity)):
                    raise ValueError('Invalid active-book replacement identity')
            if set(row) != expected_keys:
                raise ValueError('Invalid active-book stream entry')
        original_budget = _BookScanBudget(limits['bytes'], limits['entries'], cancel_check)
        replacement_budget = _BookScanBudget(limits['bytes'], limits['entries'], cancel_check)
    targets = [_get_path(root, row['path']) for row in rows]
    if (len(set(targets)) != len(targets)
            or any(target == journal or target.is_relative_to(workspace) for target in targets)
            or limits is None and any(a != b and a.is_relative_to(b) for a in targets for b in targets)):
        raise ValueError('Invalid active-book targets')
    if data['phase'] == 'committed' and limits is None:
        _validate_committed_byte_backups(workspace, rows, targets)
    elif data['phase'] == 'committed' and limits is not None:
        _verify_committed_stream_targets(rows, targets, replacement_budget)
        for index, row in enumerate(rows):
            _check_stream_cancellation(cancel_check)
            backup = workspace / f'old-{index}'
            if row.get('existed') is True and os.path.lexists(backup):
                _validate_stream_original(row, backup, original_budget)
    elif data['phase'] == 'recovered' and limits is not None:
        for row, target in zip(rows, targets):
            _check_stream_cancellation(cancel_check)
            if row.get('existed') is True:
                if not target.exists():
                    raise ValueError('Recovered book original changed; recovery evidence was preserved')
                _validate_stream_original(row, target, original_budget)
            elif row.get('existed') is not False or os.path.lexists(target):
                raise ValueError('Recovered book target changed; recovery evidence was preserved')
    if data['phase'] in ('recovered', 'committed'):
        _finish_transaction(root, workspace,
                            journal_version=journal_version,
                            stream_data=data if limits is not None else None,
                            byte_data=data if limits is None else None,
                            targets=targets if limits is None else None)
        return True
    # Validate every original and owned replacement before restoring anything.
    for index, (row, target) in enumerate(zip(rows, targets)):
        _check_stream_cancellation(cancel_check)
        original = workspace / f'old-{index}'
        discarded = workspace / f'discard-{index}'
        if row.get('existed') is True:
            candidate = original if os.path.lexists(original) else target
            if not os.path.lexists(candidate):
                raise ValueError('Active-book original artifact is missing or changed')
            if limits is not None:
                _validate_stream_original(row, candidate, original_budget)
            else:
                _validate_byte_original(row, candidate)
        elif row.get('existed') is not False or os.path.lexists(original):
            raise ValueError('Invalid active-book original ownership')
        if limits is not None and os.path.lexists(discarded):
            _validate_stream_replacement(row, discarded, replacement_budget)
            if os.path.lexists(target) and (os.path.lexists(original) or not row['existed']):
                raise ValueError('Active-book replacement appears in multiple locations')
        if os.path.lexists(target) and (os.path.lexists(original) or not row['existed']):
            # If the staged name is still present, publication did not move it
            # into this target. Matching bytes alone cannot prove ownership of
            # a file created by another writer in the meantime.
            if os.path.lexists(workspace / f'new-{index}'):
                raise ValueError('Active-book target is occupied while its staged file remains')
            if limits is not None:
                _validate_stream_replacement(row, target, replacement_budget)
            else:
                _validate_byte_replacement(row, target)
    if limits is not None:
        moved_budget = _BookScanBudget(limits['bytes'], limits['entries'], cancel_check)
    for index, (row, target) in enumerate(zip(rows, targets)):
        _check_stream_cancellation(cancel_check)
        original = workspace / f'old-{index}'
        discarded = workspace / f'discard-{index}'
        if os.path.lexists(original):
            if os.path.lexists(target):
                if limits is not None:
                    _move(target, discarded, exclusive=True, anchor=root,
                          anchor_identity=root_identity)
                    _validate_stream_replacement(row, discarded, moved_budget)
                else:
                    # Preserve the installed replacement while restoring the
                    # original. A path-based unlink can follow a swapped
                    # parent and destroys the only evidence of a concurrent
                    # writer or partial publication.
                    _move(target, discarded, exclusive=True, anchor=root,
                          anchor_identity=root_identity)
                    _validate_moved_byte_replacement(row, discarded)
            if limits is not None:
                _move(original, target, exclusive=True, anchor=root,
                      anchor_identity=root_identity)
            else:
                _move(original, target, exclusive=True, anchor=root,
                      anchor_identity=root_identity)
        elif not row['existed'] and os.path.lexists(target):
            if limits is not None:
                _move(target, discarded, exclusive=True, anchor=root,
                      anchor_identity=root_identity)
                _validate_stream_replacement(row, discarded, moved_budget)
            else:
                _move(target, discarded, exclusive=True, anchor=root,
                      anchor_identity=root_identity)
                _validate_moved_byte_replacement(row, discarded)
    if limits is not None:
        _check_stream_cancellation(cancel_check)
    data['phase'] = 'recovered'
    journal_version = _save_journal(root, data, expected_version=journal_version)
    _finish_transaction(root, workspace,
                        journal_version=journal_version,
                        stream_data=data if limits is not None else None,
                        byte_data=data if limits is None else None,
                        targets=targets if limits is None else None)
    return True


@contextlib.contextmanager
def ensure_book_state(data_dir, timeout=10, *, recover=True, cancel_check=None):
    """Serialize book operations and recover pending switches before use."""
    with file_lock(str(Path(data_dir) / JOURNAL), timeout=timeout):
        root, identity = _direct_book_root(data_dir)
        if recover:
            if cancel_check is None:
                recover_book_state_locked(root)
            else:
                recover_book_state_locked(root, cancel_check=cancel_check)
        elif os.path.lexists(root / JOURNAL):
            raise ValueError("Book recovery is required before previewing saved state")
        # Recovery may have taken time; refuse admission if the named root was
        # replaced while the operation lock was held.
        admitted_root, admitted_identity = _direct_book_root(root)
        if admitted_identity != identity:
            raise ValueError('Book root changed during admission')
        scope = _BookScope(os.path.normcase(str(admitted_root)),
                           _get_book_scope_owner(), admitted_identity)
        token = _held_books.set((*_held_books.get(), scope))
        try:
            yield
        finally:
            scope.active = False
            _held_books.reset(token)


@contextlib.contextmanager
def ensure_saved_book_state(data_dir, scripts_dir, timeout=10, *, recover=True):
    """Fence saved-book readers against an interrupted parent transaction.

    Parent transactions touching saved files must also hold this child lock.
    Once held, a pending parent journal means those files cannot safely be read
    or changed until a parent-book operation recovers it.
    """
    with ensure_book_state(scripts_dir, timeout=timeout, recover=recover):
        root = Path(data_dir)
        child = Path(scripts_dir)
        if (os.path.normcase(os.path.realpath(child.parent)) == os.path.normcase(os.path.realpath(root))
                and os.path.lexists(root / JOURNAL)):
            raise ValueError('Active-book recovery is required before accessing saved books.')
        yield


def apply_book_state_locked(data_dir, replacements, removals, *, stream_byte_limit=None, cancel_check=None):
    """Stage and publish a switch; streams require an admitted, bounded caller.

    Stream objects must already be pinned by the caller. Their extent and hash
    are checked during staging. The byte limit also bounds each aggregate scan
    of existing targets; no stream authorizes a book restore by itself.
    """
    root, root_identity = _direct_book_root(data_dir)
    if os.path.lexists(root / JOURNAL):
        raise ValueError('Recover the pending book transaction first')
    operations = list(replacements.items()) + [(name, None) for name in removals]
    streams = [value for _, value in operations if isinstance(value, BookStateStream)]
    from book_recovery_archive import MAX_FILES, _relative_name, _validate_names
    if len(operations) > MAX_FILES:
        raise ValueError('Book transaction exceeds its entry limit')
    names = [_relative_name(name) for name, _ in operations]
    _validate_names(names)
    if any(name.split('/')[0].casefold().startswith('.book-switch-') or
           name.split('/')[0].casefold() in (JOURNAL.casefold(), (JOURNAL + '.lock').casefold())
           for name in names):
        raise ValueError('Book transaction metadata is not a replacement target')
    targets = [_get_path(root, name) for name, _value in operations]
    if (not operations or len(set(targets)) != len(targets) or root / JOURNAL in targets
            or not streams and any(a != b and a.is_relative_to(b) for a in targets for b in targets)):
        raise ValueError('Duplicate or invalid book artifacts')
    if streams:
        if not is_book_state_admitted(root):
            raise ValueError('Book admission is required for streaming replacements')
        if type(stream_byte_limit) is not int or not 1 <= stream_byte_limit < 2**63:
            raise ValueError('A bounded book stream limit is required')
        stream_bytes, stream_owners = 0, set()
        for _, source in operations:
            if isinstance(source, BookStateStream):
                if (type(source.size) is not int or source.size < 0
                        or not isinstance(source.sha256, str) or len(source.sha256) != 64
                        or any(char not in '0123456789abcdef' for char in source.sha256)
                        or not callable(getattr(source.stream, 'read', None))):
                    raise ValueError('Invalid book stream identity')
                owner = id(source.stream)
                if owner in stream_owners:
                    raise ValueError('Each book stream must have an independent reader')
                stream_owners.add(owner)
                stream_bytes += source.size
            elif isinstance(source, bytes):
                stream_bytes += len(source)
            if stream_bytes > stream_byte_limit:
                raise ValueError('Book streams exceed their byte limit')
        _check_stream_cancellation(cancel_check)
        initial_budget = _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
    workspace_fd = root_fd = None
    root_guard = None
    def release_root_guard():
        nonlocal root_guard
        if root_guard is not None:
            guard, root_guard = root_guard, None
            guard.__exit__(None, None, None)
    workspace_name = '.book-switch-' + uuid.uuid4().hex
    workspace = root / workspace_name
    workspace_identity = None
    try:
        if (os.name != 'nt' and os.open in os.supports_dir_fd
                and os.mkdir in os.supports_dir_fd and hasattr(os, 'O_DIRECTORY')
                and hasattr(os, 'O_NOFOLLOW')):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(root, flags)
            opened_root = os.fstat(root_fd)
            current_root = root.lstat()
            if ((opened_root.st_dev, opened_root.st_ino) != root_identity
                    or (current_root.st_dev, current_root.st_ino) != root_identity):
                raise ValueError('Book root changed before staging')
            os.mkdir(workspace_name, mode=0o700, dir_fd=root_fd)
            workspace_fd = os.open(workspace_name, flags, dir_fd=root_fd)
        else:
            if os.name == 'nt':
                from windows_export_staging_directory import WindowsBookRoot
                root_guard = WindowsBookRoot(root, root_identity)
                root_guard.__enter__()
            workspace = Path(tempfile.mkdtemp(prefix='.book-switch-', dir=root))
            workspace_name = workspace.name
        workspace_info = (os.fstat(workspace_fd) if workspace_fd is not None
                          else workspace.lstat())
        workspace_identity = (workspace_info.st_dev, workspace_info.st_ino)
    except BaseException:
        if root_fd is not None and workspace_identity is None:
            try:
                info = os.stat(workspace_name, dir_fd=root_fd, follow_symlinks=False)
                workspace_identity = (info.st_dev, info.st_ino)
            except FileNotFoundError:
                pass
        if workspace_identity is not None:
            _cleanup_unpublished_stream_workspace(workspace, workspace_identity, {},
                                                  parent_fd=root_fd)
        if workspace_fd is not None:
            os.close(workspace_fd)
        if root_fd is not None:
            os.close(root_fd)
        if root_guard is not None:
            root_guard.__exit__(None, None, None)
        raise
    staged_identities = {}
    staged_versions = {}
    parent_identities = None
    rows, target_versions = [], []
    published = False
    publication_started = False
    try:
        if root_fd is not None:
            parent_identities = [
                _capture_target_parent_identities(root_fd, name) for name in names]
        for index, ((name, value), target) in enumerate(zip(operations, targets)):
            if value is not None and not isinstance(value, (bytes, BookStateStream)):
                raise ValueError('Book replacements must be bytes')
            if value is not None and target.is_dir():
                raise ValueError('Cannot replace a directory with a book file')
            existed = os.path.lexists(target)
            before_version = _book_target_version(target) if existed else None
            if existed:
                if stat.S_ISDIR(before_version[5]):
                    _cleanup_tree_inventory(target)
                elif not stat.S_ISREG(before_version[5]) or before_version[6] != 1:
                    raise ValueError('Book artifact cannot be safely retired')
            row = {'path':name, 'existed':existed,
                   'old_sha256':(_bounded_book_digest(target, initial_budget) if streams else _get_digest(target))
                                if existed else None,
                   'new_sha256':(value.sha256 if isinstance(value, BookStateStream) else
                                 hashlib.sha256(value).hexdigest() if value is not None else None)}
            if existed:
                row['old_file_identity'] = [before_version[item] for item in (0, 1, 2, 3, 5, 6)]
            if existed and _book_target_version(target) != before_version:
                raise ValueError('Book artifact changed during staging')
            target_versions.append(before_version)
            if value is not None:
                mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o600
                staged_name = f'new-{index}'
                staged_path = workspace / staged_name
                def record_staged(info, staged_name=staged_name, staged_path=staged_path):
                    # CPython's Windows inode for a newly opened file may
                    # change after close. Bind failed-stage cleanup to the
                    # native file ID, then bind publication to the stable
                    # named identity only after staging succeeds below.
                    if os.name == 'nt':
                        from windows_export_staging_directory import windows_named_file_identity
                        staged_identities[staged_name] = windows_named_file_identity(staged_path)
                    else:
                        staged_identities[staged_name] = (info.st_dev, info.st_ino)
                def record_finished(info, staged_name=staged_name):
                    if os.name == 'nt' and (info.st_dev, info.st_ino) != staged_identities.get(staged_name):
                        raise ValueError('Book staging file changed after writing')
                    staged_versions[staged_name] = _book_info_version(info)
                if streams:
                    source = (value if isinstance(value, BookStateStream) else
                              BookStateStream(io.BytesIO(value), len(value), row['new_sha256']))
                    _stage_book_stream(staged_name if workspace_fd is not None else staged_path,
                                       source, mode, cancel_check, on_open=record_staged,
                                       on_finish=record_finished,
                                       directory_fd=workspace_fd)
                    if os.name == 'nt':
                        info = staged_path.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or getattr(info, 'st_file_attributes', 0)
                                & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                            raise ValueError('Unsafe Windows book staging file')
                        row['new_file_identity'] = [info.st_dev, info.st_ino]
                else:
                    if workspace_fd is not None:
                        source = BookStateStream(io.BytesIO(value), len(value), row['new_sha256'])
                        _stage_book_stream(staged_name, source, mode, None,
                                           on_open=record_staged, on_finish=record_finished,
                                           directory_fd=workspace_fd)
                    else:
                        save_adapter_publication_bytes(str(staged_path), value, mode)
                        info = staged_path.lstat()
                        if os.name == 'nt':
                            from windows_export_staging_directory import windows_named_file_identity
                            staged_identities[staged_name] = windows_named_file_identity(staged_path)
                            row['new_file_identity'] = [info.st_dev, info.st_ino]
                        else:
                            staged_identities[staged_name] = (info.st_dev, info.st_ino)
                if 'new_file_identity' not in row:
                    row['new_file_identity'] = list(staged_identities[staged_name])
                if workspace_fd is not None or streams:
                    named_version = (_book_info_version(os.stat(
                        staged_name, dir_fd=workspace_fd, follow_symlinks=False))
                        if workspace_fd is not None else _book_target_version(staged_path))
                    if named_version != staged_versions.get(staged_name):
                        raise ValueError('Book staging file changed after writing')
                else:
                    staged_versions[staged_name] = _book_target_version(staged_path)
            rows.append(row)
        if streams:
            _check_stream_cancellation(cancel_check)
            verification_budget = _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
            # A large streamed input can take a while to stage. Do not replace
            # a destination edited during that interval, even if its book token
            # stayed the same. The normal book/task locks remain required.
            for row, target, old_version in zip(rows, targets, target_versions):
                _check_stream_cancellation(cancel_check)
                _validate_stream_target(row, target, old_version, verification_budget)
        data = {'version':2 if streams else 3, 'phase':'publishing', 'workspace':workspace.name, 'entries':rows}
        if streams:
            data['stream_limits'] = {'bytes':stream_byte_limit, 'entries':MAX_FILES}
        if parent_identities is not None:
            data['target_parent_identities'] = parent_identities
        if root_guard is not None:
            root_guard.check()
        journal_version = _save_journal(root, data, create=True, root_fd=root_fd)
        published = True
        if streams:
            move_budget = _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
            moved_budget = _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
        # Validate every staged entry before moving the first original aside.
        # Recheck each entry again immediately before its own publication.
        staged_budget = (_BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
                         if streams else None)
        for index, row in enumerate(rows):
            _check_stream_cancellation(cancel_check)
            _validate_staged_replacement(row, workspace / f'new-{index}', staged_budget)
        for index, (row, target) in enumerate(zip(rows, targets)):
            if parent_identities is not None:
                _require_transaction_parent_identities(
                    root, names, parent_identities, root_fd=root_fd)
            if streams:
                _check_stream_cancellation(cancel_check)
                _validate_stream_target(row, target, target_versions[index], move_budget)
            current_root, current_identity = _direct_book_root(root)
            if current_identity != root_identity or current_root != root:
                raise ValueError('Book root changed before publication')
            _validate_staged_replacement(
                row, workspace / f'new-{index}',
                _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check) if streams else None)
            if row['existed']:
                publication_started = True
                original = workspace / f'old-{index}'
                if streams:
                    _move(target, original, exclusive=True, anchor=root,
                          anchor_identity=root_identity, root_guard=root_guard)
                else:
                    _move(target, original, exclusive=True, anchor=root,
                          anchor_identity=root_identity, root_guard=root_guard)
                if streams:
                    _validate_moved_stream_original(row, original, target_versions[index], moved_budget)
                else:
                    # Preserve an intervening edit in the transaction workspace.
                    # Recovery must not treat that changed backup as ours to
                    # discard or silently overwrite it with the staged result.
                    _validate_moved_byte_original(row, original, target_versions[index])
            staged = workspace / f'new-{index}'
            if os.path.lexists(staged):
                publication_started = True
                if streams:
                    _move(staged, target, exclusive=True, anchor=root,
                          anchor_identity=root_identity, root_guard=root_guard)
                else:
                    # A writer may create the destination after its original
                    # was backed up. Never replace that new file silently.
                    _move(staged, target, exclusive=True, anchor=root,
                          anchor_identity=root_identity, root_guard=root_guard)
            if parent_identities is not None:
                _require_transaction_parent_identities(
                    root, names, parent_identities, root_fd=root_fd)
        if streams:
            _verify_committed_stream_targets(rows, targets,
                _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check))
            backup_budget = _BookScanBudget(stream_byte_limit, MAX_FILES, cancel_check)
            for index, row in enumerate(rows):
                _check_stream_cancellation(cancel_check)
                if row['existed']:
                    _validate_moved_stream_original(row, workspace / f'old-{index}',
                                                    target_versions[index], backup_budget)
        else:
            # Recheck backups before committing. This catches edits made through
            # previously opened descriptors after the move; it cannot exclude
            # a non-cooperating writer after the final check.
            for index, row in enumerate(rows):
                if row['existed']:
                    _validate_moved_byte_original(row, workspace / f'old-{index}', target_versions[index])
            _validate_byte_cleanup_targets(rows, targets, 'committed')
        if parent_identities is not None:
            _require_transaction_parent_identities(
                root, names, parent_identities, root_fd=root_fd)
        data['phase'] = 'committed'
        if root_guard is not None:
            root_guard.check()
        journal_version = _save_journal(root, data, expected_version=journal_version,
                                        root_fd=root_fd)
        if streams:
            _verify_committed_stream_targets(rows, targets,
                _BookScanBudget(stream_byte_limit, MAX_FILES, None))
        else:
            _validate_committed_byte_backups(workspace, rows, targets)
        release_root_guard()
        _finish_transaction(root, workspace,
                            journal_version=journal_version,
                            stream_data=data if streams else None,
                            byte_data=data if not streams else None,
                            targets=targets if not streams else None)
    except BaseException as original_error:
        if published:
            if not publication_started:
                release_root_guard()
                if streams:
                    _finish_transaction(root, workspace, journal_version=journal_version,
                                        stream_data=data)
                else:
                    try:
                        _cleanup_unpublished_stream_workspace(
                            workspace, workspace_identity, staged_identities, parent_fd=root_fd,
                            staged_versions=staged_versions, require_stage_versions=streams)
                        _unlink_owned_book_journal(root, journal_version, root_fd=root_fd)
                    except (OSError, ValueError):
                        # Preserve evidence when staged ownership changed, but
                        # do not replace the causal byte-publication error.
                        pass
            else:
                release_root_guard()
                recover_book_state_locked(data_dir)
        elif (root_fd is not None or os.path.lexists(workspace)):
            try:
                _cleanup_unpublished_stream_workspace(
                    workspace, workspace_identity, staged_identities,
                    parent_fd=root_fd, staged_versions=staged_versions,
                    require_stage_versions=streams)
            except (OSError, ValueError) as cleanup_error:
                # Keep the safety refusal as the primary error while making
                # the staging failure explicit; the workspace is retained.
                raise cleanup_error from original_error
        raise
    finally:
        if workspace_fd is not None:
            os.close(workspace_fd)
        if root_fd is not None:
            os.close(root_fd)
        release_root_guard()



def get_book_operation_identity(data_dir, *, state=None):
    """Read the selected book generation independently of edits within the book."""
    root = Path(data_dir)
    if state is None:
        state_path = root / "state.json"
        state = json.loads(state_path.read_bytes()) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("Invalid book identity")
    return [os.path.abspath(data_dir), state.get("active_book_id"), state.get("book_generation")]


def get_book_operation_token(data_dir, *, state=None):
    """Identify the current selection using the editor's existing identity digest."""
    return get_snapshot_digest(get_book_operation_identity(data_dir, state=state))


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



def apply_book_input_selection(data_dir, input_path, book_id, *, timeout=10, before_publication=None):
    """Select an input under the same book-operation contract as load/jobs.

    `before_publication` notifies an upload owner after validation and before
    the first write. After that handoff, errors may have an unknown commit
    outcome; uploaded source/provenance must remain available for recovery.
    """
    from utils import safe_load_json
    from sound_design_registry import ACTIVE_CUES
    from sound_design_renditions import ACTIVE_RENDITIONS, capture_sound_design_companions
    with ensure_book_state(data_dir, timeout=timeout):
        state_path = Path(data_dir) / "state.json"
        with file_lock(str(state_path)):
            state = safe_load_json(str(state_path), default={})
            if not isinstance(state, dict):
                raise ValueError("Book state must be an object")
            if input_path is None:
                input_path = state.get("input_file_path")
            previous_book_id = state.get('active_book_id')
            state.update(input_file_path=input_path, active_book_id=book_id,
                         book_generation=uuid.uuid4().hex)
            # Selecting a source does not replace the previous script. Keep it
            # recoverable, but do not certify it as belonging to this selection.
            state['script_selection_pending'] = True
            state.pop('_source_scope', None)
            state.pop('_source_scope_exports', None)
            from speech_numbers import STATE_KEY as date_order_key
            state.pop(date_order_key, None)        # a new book starts on auto (#1546)
            # An explicit input selection starts a new book context. Validate
            # optional old metadata before clearing it in the same transaction;
            # malformed/recoverable files must not disappear on a failed switch.
            companions = [name for name in (ACTIVE_CUES, ACTIVE_RENDITIONS)
                          if os.path.lexists(Path(data_dir) / name)]
            if companions:
                capture_sound_design_companions(data_dir, ACTIVE_CUES, ACTIVE_RENDITIONS,
                                                expected_book_id=previous_book_id)
            if before_publication is not None:
                before_publication()
            if companions:
                apply_book_state_locked(data_dir, {
                    'state.json': json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8')},
                    companions)
            else:
                save_adapter_publication_bytes(str(state_path), json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))
            return get_book_operation_token(data_dir)



def get_book_snapshot_token(snapshot):
    """Identify a book generation/source independently of editable voice fields."""
    payload = {"identity":snapshot["identity"], "script_sha256":snapshot["script_sha256"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
