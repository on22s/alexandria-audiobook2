"""Registered disposable synthesis spill, separate from reusable audio evidence.

Only exact live Project admission creates workspaces. Abandoned reclamation is
called under the existing global task-idle barrier and canonical book lock.
Unsupported/unleased callers retain their ordinary temporary-file lifetime.
"""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import itertools
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from types import MappingProxyType

from utils import file_lock

DIRECTORY = '.synthesis-workspaces-v1'
INDEX = 'registry.json'
PENDING = 'registry.pending'
MAX_ROWS = 256
MAX_WINDOWS = 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_ENCODER_TEMP_FILES = 16
MAX_ENCODER_TEMP_BYTES = 8 * 1024 ** 3
_ENCODED_FILES = {'encoded-parent.mp3', 'encoded-parent.wav', 'input.pcm',
                  'output.mp3', 'output.wav', 'encoder-error.json'}
_encoding_outputs = ContextVar('registered_encoding_outputs', default=None)
LOCAL_CATEGORIES = ('custom', 'clone', 'design', 'lora', 'ensemble')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
# A writer that publishes through `<output>.pending.<uuid4 hex>` (local design)
# leaves this beside a declared window or the WAV parent on process death.
_OUTPUT_STAGING = re.compile(r'(window-[0-9]+\.wav|assembled-parent\.wav)\.pending\.[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_LOG = logging.getLogger(__name__)


class SynthesisWorkspaceUnavailable(RuntimeError):
    """Disposable storage could not be admitted; never spill around its limit."""


def is_synthesis_workspace_supported():
    # Directory sync and unlink/no-follow semantics are currently qualified on
    # Linux. This must not turn unsupported native platforms into startup errors.
    return (sys.platform == 'linux' and hasattr(os, 'O_DIRECTORY')
            and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd)


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SynthesisWorkspaceUnavailable('Duplicate workspace registry key')
        value[key] = item
    return value


def _identity(info):
    return [info.st_dev, info.st_ino]


def _validate(body):
    if (type(body) is not dict or set(body) != {'version', 'rows'}
            or type(body['version']) is not int or body['version'] != 1
            or type(body['rows']) is not dict or len(body['rows']) > MAX_ROWS):
        raise SynthesisWorkspaceUnavailable('Invalid workspace registry')
    for name, row in body['rows'].items():
        if (not isinstance(name, str) or not _HEX32.fullmatch(name)
                or type(row) is not dict
                or set(row) not in ({'claim', 'book', 'windows', 'identity', 'join_identity'},
                                    {'claim', 'book', 'windows', 'identity', 'join_identity', 'encoding'})
                or any(not isinstance(row[key], str) or not _HEX64.fullmatch(row[key]) for key in ('claim', 'book'))
                or type(row['windows']) is not int
                or not (row['windows'] == 0 or 2 <= row['windows'] <= MAX_WINDOWS)):
            raise SynthesisWorkspaceUnavailable('Invalid workspace row')
        for key in ('identity', 'join_identity'):
            value = row[key]
            if value is not None and (type(value) is not list or len(value) != 2
                    or any(type(item) is not int or item < 0 for item in value)):
                raise SynthesisWorkspaceUnavailable('Invalid workspace directory identity')
        if (row['identity'] is None) != (row['join_identity'] is None):
            raise SynthesisWorkspaceUnavailable('Incomplete workspace creation identity')
        if 'encoding' in row and (row['encoding'] is not True or row['windows'] != 0
                                   or row['identity'] is None):
            raise SynthesisWorkspaceUnavailable('Invalid encoding workspace authority')


class SynthesisWorkspaceRegistry:
    """One locked registry transaction; caller acquires live/idle authority first.

    No owner callbacks or task admission while this lock is held. All data IO is
    relative to pinned directories. A malformed existing authority is preserved.
    """
    def __init__(self, root, *, create=False, expected_root_identity=None):
        self.root = Path(root)
        self.create = create
        if expected_root_identity is not None and (type(expected_root_identity) is not tuple
                or len(expected_root_identity) != 2
                or any(type(value) is not int or value < 0 for value in expected_root_identity)):
            raise SynthesisWorkspaceUnavailable('Invalid captured workspace root identity')
        self.expected_root_identity = expected_root_identity
        self.parent_fd = self.fd = None
        self._lock_stream = self._lock_context = None
        self.body = None

    def __enter__(self):
        try:
            self.parent_fd = os.open(self.root.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if (self.expected_root_identity is not None
                    and tuple(_identity(os.fstat(self.parent_fd))) != self.expected_root_identity):
                raise SynthesisWorkspaceUnavailable('Captured workspace root changed')
            created = False
            if self.create:
                try:
                    os.mkdir(DIRECTORY, 0o700, dir_fd=self.parent_fd)
                    created = True
                    os.fsync(self.parent_fd)
                except FileExistsError:
                    pass
            self.fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self._check_root()
            self._lock_stream = os.fdopen(self._open(INDEX + '.lock', os.O_RDWR | os.O_CREAT), 'r+b')
            lock_context = file_lock(self.root / DIRECTORY / INDEX, timeout=0, lock_handle=self._lock_stream)
            lock_context.__enter__()
            self._lock_context = lock_context
            self._check_root()
            if created:
                self.body = {'version': 1, 'rows': {}}
                self.save()
            else:
                self.body = self._load()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *error):
        try:
            if self._lock_context is not None:
                context, self._lock_context = self._lock_context, None
                context.__exit__(*error)
        finally:
            if self._lock_stream is not None:
                self._lock_stream.close()
                self._lock_stream = None
            for key in ('fd', 'parent_fd'):
                fd = getattr(self, key)
                if fd is not None:
                    setattr(self, key, None)
                    os.close(fd)

    def _check_directory(self, info):
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise SynthesisWorkspaceUnavailable('Unsafe workspace directory')

    def _check_root(self):
        current = os.stat(DIRECTORY, dir_fd=self.parent_fd, follow_symlinks=False)
        self._check_directory(current)
        if _identity(current) != _identity(os.fstat(self.fd)):
            raise SynthesisWorkspaceUnavailable('Workspace namespace changed')
        if self._lock_stream is not None:
            current_lock = os.stat(INDEX + '.lock', dir_fd=self.fd, follow_symlinks=False)
            if (not stat.S_ISREG(current_lock.st_mode) or current_lock.st_nlink != 1
                    or current_lock.st_uid != os.geteuid() or current_lock.st_mode & 0o077
                    or _identity(current_lock) != _identity(os.fstat(self._lock_stream.fileno()))):
                raise SynthesisWorkspaceUnavailable('Workspace registry lock changed')

    def _open(self, name, flags):
        self._check_root()
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_INDEX_BYTES):
                raise SynthesisWorkspaceUnavailable('Unsafe workspace registry file')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _load(self):
        with os.fdopen(self._open(INDEX, os.O_RDONLY), 'rb') as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            raise SynthesisWorkspaceUnavailable('Workspace registry too large')
        try:
            value = json.loads(raw, object_pairs_hook=_unique)
            if type(value) is not dict or set(value) != {'body', 'sha256'}:
                raise ValueError('Invalid envelope')
            _validate(value['body'])
            if value['sha256'] != hashlib.sha256(_bytes(value['body'])).hexdigest():
                raise ValueError('Checksum mismatch')
            return value['body']
        except (ValueError, TypeError, RecursionError, UnicodeError) as error:
            raise SynthesisWorkspaceUnavailable('Invalid workspace registry bytes') from error

    def save(self):
        _validate(self.body)
        data = _bytes(dict(body=self.body, sha256=hashlib.sha256(_bytes(self.body)).hexdigest()))
        if len(data) > MAX_INDEX_BYTES:
            raise SynthesisWorkspaceUnavailable('Workspace registry capacity exceeded')
        with os.fdopen(self._open(PENDING, os.O_RDWR | os.O_CREAT), 'r+b') as stream:
            stream.truncate(0)
            if stream.write(data) != len(data):
                raise OSError('Short workspace registry write')
            stream.flush()
            os.fsync(stream.fileno())
        self._check_root()
        os.replace(PENDING, INDEX, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self._check_root()

    def reserve(self, parent, windows, *, encoding=False):
        if type(encoding) is not bool or (encoding and windows != 0):
            raise SynthesisWorkspaceUnavailable('Encoding requires a raw parent workspace')
        if len(self.body['rows']) >= MAX_ROWS:
            raise SynthesisWorkspaceUnavailable('Synthesis workspace capacity exhausted; idle recovery is required')
        name = uuid.uuid4().hex
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SynthesisWorkspaceUnavailable('Workspace name is already occupied')
        row = dict(claim=parent.claim_sha256, book=parent.book_sha256,
                   windows=windows, identity=None, join_identity=None)
        self.body['rows'][name] = row
        self.save()  # No directory or audio before this durable reservation.
        os.mkdir(name, 0o700, dir_fd=self.fd)
        os.fsync(self.fd)
        workspace_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            os.mkdir('join', 0o700, dir_fd=workspace_fd)
            join_fd = os.open('join', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
            try:
                os.fsync(join_fd)
                os.fsync(workspace_fd)
                row.update(identity=_identity(os.fstat(workspace_fd)), join_identity=_identity(os.fstat(join_fd)))
                if encoding:
                    row['encoding'] = True
                self.save()  # Provider may write only after both identities commit.
            finally:
                os.close(join_fd)
        finally:
            os.close(workspace_fd)
        return name, dict(row)

    def retire(self, name, expected=None):
        row = self.body['rows'].get(name)
        if row is None:
            return False
        if expected is not None and row != expected:
            raise SynthesisWorkspaceUnavailable('Workspace registration changed')
        self._check_root()
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            os.fsync(self.fd)
        else:
            self._check_directory(info)
            if row['identity'] is None or _identity(info) != row['identity']:
                raise SynthesisWorkspaceUnavailable('Unproven workspace creation or changed directory')
            self._delete_contents(name, row)
        del self.body['rows'][name]
        self.save()
        return True

    def create_encoded_parent(self, name, suffix):
        """Return an exclusive output descriptor inside a pinned registration.

        Caller owns admission while opening and closes the returned descriptor.
        Copying can then release the task mutex without following mutable paths.
        """
        if suffix not in ('.wav', '.mp3', '.flac', '.ogg'):
            raise SynthesisWorkspaceUnavailable('Unsupported encoded parent format')
        row = self.body['rows'].get(name)
        if row is None or row['identity'] is None:
            raise SynthesisWorkspaceUnavailable('Encoded parent has no registered workspace')
        self._check_root()
        workspace_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            if _identity(os.fstat(workspace_fd)) != row['identity']:
                raise SynthesisWorkspaceUnavailable('Workspace directory changed before parent creation')
            return os.open('assembled-parent' + suffix,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=workspace_fd)
        finally:
            os.close(workspace_fd)

    def _delete_contents(self, name, row):
        workspace_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        join_fd = None
        try:
            if _identity(os.fstat(workspace_fd)) != row['identity']:
                raise SynthesisWorkspaceUnavailable('Workspace directory changed')
            windows = {f'window-{index}.wav' for index in range(row['windows'])}
            allowed = (windows
                       | {f'assembled-parent{suffix}' for suffix in ('.wav', '.mp3', '.flac', '.ogg')}
                       | {'join'})
            if row.get('encoding') is True:
                allowed |= _ENCODED_FILES
            # Each declared window and the WAV parent may hold one staging file.
            staging_outputs = windows | {'assembled-parent.wav'}
            entries = self._get_names(workspace_fd, len(allowed) + len(staging_outputs))
            for child in entries:
                staging = _OUTPUT_STAGING.fullmatch(child)
                if child not in allowed and not (staging and staging.group(1) in staging_outputs):
                    raise SynthesisWorkspaceUnavailable('Unknown workspace contents preserved')
            if 'join' in entries:
                info = os.stat('join', dir_fd=workspace_fd, follow_symlinks=False)
                self._check_directory(info)
                if _identity(info) != row['join_identity']:
                    raise SynthesisWorkspaceUnavailable('Join directory changed')
                join_fd = os.open('join', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
                if _identity(os.fstat(join_fd)) != row['join_identity']:
                    raise SynthesisWorkspaceUnavailable('Join directory changed')
                encoding = row.get('encoding') is True
                joined = self._get_names(join_fd, MAX_ENCODER_TEMP_FILES if encoding else 1)
                if not encoding and not set(joined) <= {'complete.wav'}:
                    raise SynthesisWorkspaceUnavailable('Unknown join contents preserved')
                total = 0
                for child in joined:
                    info = self._check_file(join_fd, child)
                    total += info.st_size
                if encoding and total > MAX_ENCODER_TEMP_BYTES:
                    raise SynthesisWorkspaceUnavailable('Encoder scratch byte bound exceeded')
            else:
                joined = []
            # Validate the entire declared tree before deleting even one byte.
            for child in entries:
                if child != 'join':
                    self._check_file(workspace_fd, child)
            for child in joined:
                os.unlink(child, dir_fd=join_fd)
            if join_fd is not None:
                os.fsync(join_fd)
                os.rmdir('join', dir_fd=workspace_fd)
            for child in entries:
                if child != 'join':
                    os.unlink(child, dir_fd=workspace_fd)
            os.fsync(workspace_fd)
            self._check_root()
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if _identity(current) != row['identity']:
                raise SynthesisWorkspaceUnavailable('Workspace directory changed before retirement')
            os.rmdir(name, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            if join_fd is not None:
                os.close(join_fd)
            os.close(workspace_fd)

    @staticmethod
    def _get_names(fd, limit):
        with os.scandir(fd) as entries:
            names = [entry.name for entry in itertools.islice(entries, limit + 1)]
        if len(names) > limit:
            raise SynthesisWorkspaceUnavailable('Workspace child limit exceeded')
        return names

    @staticmethod
    def _check_file(fd, name):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise SynthesisWorkspaceUnavailable('Unsafe workspace file preserved')
        return info


@dataclass
class _EncodingLifetime:
    active: bool = True
    completed: dict = field(default_factory=dict)
    created: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RegisteredEncodingStage:
    """Live, non-serialized authority for this parent's disposable encoding."""
    parent: object
    owner: object
    directory: Path
    root_identity: tuple
    namespace_identity: tuple
    identity: tuple
    join_identity: tuple
    raw_paths: tuple
    lifetime: _EncodingLifetime

    def _assert_context(self):
        from audio_generation_owner import get_audio_generation_owner
        from synthesis_recovery_context import get_recovery_claim_digest
        if (not self.lifetime.active or get_audio_generation_owner() is not self.owner
                or get_recovery_claim_digest() != self.parent.claim_sha256
                or os.path.realpath(self.parent.project_root) != str(self.directory.parent.parent)):
            raise SynthesisWorkspaceUnavailable('Encoding workspace owner expired')

    @contextmanager
    def _opened_directories(self):
        """Pinned metadata access; mutation callers also hold live admission."""
        self._assert_context()
        descriptors = []
        entries = ((self.directory.parent.parent, self.root_identity),
                   (DIRECTORY, self.namespace_identity),
                   (self.directory.name, self.identity), ('join', self.join_identity))
        try:
            for name, expected in entries:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 **({'dir_fd': descriptors[-1]} if descriptors else {}))
                except OSError as error:
                    raise SynthesisWorkspaceUnavailable('Encoding directory changed or is unavailable') from error
                descriptors.append(fd)
                info = os.fstat(fd)
                if tuple(_identity(info)) != expected:
                    raise SynthesisWorkspaceUnavailable('Encoding directory identity changed')
                if len(descriptors) > 1 and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                    raise SynthesisWorkspaceUnavailable('Encoding directory is not private')
            yield descriptors[-2]
            for index, (name, expected) in enumerate(entries):
                try:
                    info = os.stat(name, follow_symlinks=False,
                                   **({'dir_fd': descriptors[index-1]} if index else {}))
                except OSError as error:
                    raise SynthesisWorkspaceUnavailable('Encoding directory changed during operation') from error
                if (not stat.S_ISDIR(info.st_mode) or tuple(_identity(info)) != expected
                        or index and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
                    raise SynthesisWorkspaceUnavailable('Encoding directory changed during operation')
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    @contextmanager
    def _directories(self):
        self._assert_context()
        with self.parent.admit(), self._opened_directories() as fd:
            yield fd

    def create_file(self, name):
        if name not in _ENCODED_FILES:
            raise SynthesisWorkspaceUnavailable('Unknown encoded output')
        descriptor = None
        try:
            with self._directories() as fd:
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                identity = tuple(_identity(os.fstat(descriptor)))
            self.lifetime.created[name] = identity
            result, descriptor = descriptor, None
            return result
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def require_fresh_encoding(self):
        with self._directories() as fd:
            for name in _ENCODED_FILES:
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise SynthesisWorkspaceUnavailable('Encoder member already exists before encoding')

    def export_layout(self, destination, format_name):
        if format_name not in ('mp3', 'wav') or Path(destination) != self.directory / ('encoded-parent.' + format_name):
            raise SynthesisWorkspaceUnavailable('Encoding destination differs from its authority')
        self.check_member(Path(destination).name)
        return self.directory, self.directory / 'join'

    def check_member(self, name, identity=None):
        if name not in _ENCODED_FILES | {'assembled-parent.wav'}:
            raise SynthesisWorkspaceUnavailable('Unknown encoding member')
        if identity is None:
            completed = self.lifetime.completed.get(name)
            identity = completed[:2] if completed is not None else self.lifetime.created.get(name)
        with self._directories() as fd:
            try:
                info = SynthesisWorkspaceRegistry._check_file(fd, name)
            except OSError as error:
                raise SynthesisWorkspaceUnavailable('Encoding file changed or is unavailable') from error
            if identity is not None and tuple(_identity(info)) != tuple(identity):
                raise SynthesisWorkspaceUnavailable('Encoding file identity changed')
            return info

    @contextmanager
    def open_parent(self):
        source = None
        try:
            with self._directories() as fd:
                before = SynthesisWorkspaceRegistry._check_file(fd, 'assembled-parent.wav')
                source = os.open('assembled-parent.wav', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, 'rb') as reader:
                source = None
                if _encoding_file_version(os.fstat(reader.fileno())) != _encoding_file_version(before):
                    raise SynthesisWorkspaceUnavailable('Raw parent changed before encoding')
                yield reader
                after = os.fstat(reader.fileno())
                named = self.check_member('assembled-parent.wav')
                if (_encoding_file_version(before) != _encoding_file_version(after)
                        or _encoding_file_version(before) != _encoding_file_version(named)):
                    raise SynthesisWorkspaceUnavailable('Raw parent changed during encoding')
        finally:
            if source is not None:
                os.close(source)

    def finish_encoder_output(self, source_name, destination_name, source_version, destination_identity):
        source = None
        try:
            with self._directories() as fd:
                if (source_name not in ('output.mp3', 'output.wav')
                        or destination_name != 'encoded-parent.' + source_name.split('.')[1]):
                    raise SynthesisWorkspaceUnavailable('Invalid encoded completion names')
                source = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                before = os.fstat(source)
                if _encoding_file_version(before) != source_version:
                    raise SynthesisWorkspaceUnavailable('Encoder-written output changed')
                target = SynthesisWorkspaceRegistry._check_file(fd, destination_name)
                if tuple(_identity(target)) != tuple(destination_identity):
                    raise SynthesisWorkspaceUnavailable('Encoded destination identity changed')
                _RAW_ENCODING_CHECKS[0](fd, source_name, source, source_version)
                os.replace(source_name, destination_name, src_dir_fd=fd, dst_dir_fd=fd)
                written = _RAW_ENCODING_CHECKS[1](destination_name, source, source_version, fd)
            self.lifetime.completed[destination_name] = written
        except OSError as error:
            raise SynthesisWorkspaceUnavailable('Encoded completion file changed or is unavailable') from error
        finally:
            if source is not None:
                os.close(source)

    def check_completed(self, path):
        name = Path(path).name
        expected = self.lifetime.completed.get(name)
        if Path(path) != self.directory / name or expected is None:
            raise SynthesisWorkspaceUnavailable('Output is not the completed registered encoding')
        actual = _encoding_file_version(self.check_member(name))
        if actual != expected:
            raise SynthesisWorkspaceUnavailable('Completed encoding changed')
        return expected

    def completed_path(self, relative):
        for name in self.lifetime.completed:
            path = self.directory / name
            if relative == os.path.relpath(path, self.directory.parent.parent):
                self.check_completed(path)
                return str(path)
        raise SynthesisWorkspaceUnavailable('Encoder returned an unexpected registered output')

    @contextmanager
    def open_completed(self, path):
        expected = self.check_completed(path)
        directory_fd = source = root_fd = destination_fd = None
        holder = None
        try:
            with self._directories() as fd:
                directory_fd = os.dup(fd)
                source = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                _RAW_ENCODING_CHECKS[0](fd, Path(path).name, source, expected)
                root_fd = os.open(self.directory.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if tuple(_identity(os.fstat(root_fd))) != self.root_identity:
                    raise SynthesisWorkspaceUnavailable('Registered publication root changed')
                destination_fd = os.open('voicelines', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            from audio_validation import _RAW_PUBLISH_AUDIO_OUTPUT
            holder = RegisteredEncodingSource(self, str(path), directory_fd, source, expected,
                root_fd, destination_fd, tuple(_identity(os.fstat(destination_fd))), _RAW_PUBLISH_AUDIO_OUTPUT)
            yield holder
        finally:
            if holder is not None:
                holder.active = False
            for fd in (source, directory_fd, destination_fd, root_fd):
                if fd is not None:
                    os.close(fd)

    def copy_wav(self):
        from audio_generation_owner import ensure_audio_generation_active
        target = self.directory / 'encoded-parent.wav'
        output = self.create_file(target.name)
        try:
            with ExitStack() as handles:
                writer = handles.enter_context(os.fdopen(output, 'wb'))
                output = None
                reader = handles.enter_context(self.open_parent())
                before = os.fstat(reader.fileno())
                size = 0
                source_digest = hashlib.sha256()
                while True:
                    ensure_audio_generation_active()
                    block = reader.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > before.st_size:
                        raise SynthesisWorkspaceUnavailable('Raw parent grew during encoding')
                    writer.write(block)
                    source_digest.update(block)
                if size != before.st_size:
                    raise SynthesisWorkspaceUnavailable('Raw parent truncated during encoding')
                writer.flush()
                written = os.fstat(writer.fileno())
                if written.st_size != before.st_size:
                    raise SynthesisWorkspaceUnavailable('Encoded WAV copy is incomplete')
                target_digest = hashlib.sha256()
                offset = 0
                while offset < written.st_size:
                    ensure_audio_generation_active()
                    block = os.pread(writer.fileno(), min(1024 * 1024, written.st_size - offset), offset)
                    if not block:
                        raise SynthesisWorkspaceUnavailable('Encoded WAV copy ended during verification')
                    target_digest.update(block)
                    offset += len(block)
                if (target_digest.digest() != source_digest.digest()
                        or _encoding_file_version(os.fstat(writer.fileno())) != _encoding_file_version(written)):
                    raise SynthesisWorkspaceUnavailable('Encoded WAV copy changed')
                named = self.check_member(target.name)
                if _encoding_file_version(written) != _encoding_file_version(named):
                    raise SynthesisWorkspaceUnavailable('Encoded WAV replaced during encoding')
            self.lifetime.completed[target.name] = _encoding_file_version(written)
            return str(target)
        finally:
            if output is not None:
                os.close(output)


def _encoding_file_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink, info.st_uid)


def _check_encoding_source(directory_fd, name, source_fd, expected):
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(source_fd)
    except OSError as error:
        raise SynthesisWorkspaceUnavailable('Registered publication source is unavailable') from error
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_uid != os.geteuid()
            or _encoding_file_version(opened) != expected or _encoding_file_version(named) != expected):
        raise SynthesisWorkspaceUnavailable('Registered publication source changed')


def _check_encoding_destination(path, source_fd, before, directory_fd=None):
    try:
        written = os.fstat(source_fd)
        named = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise SynthesisWorkspaceUnavailable('Registered publication destination is unavailable') from error
    actual = _encoding_file_version(written)
    # Rename changes ctime; every other field must still describe the intended
    # source, and the destination must name that very open inode. Preserve a
    # mismatch for inspection rather than deleting an unknown replacement.
    if (actual[:4] + actual[5:] != before[:4] + before[5:]
            or _encoding_file_version(named) != actual):
        raise SynthesisWorkspaceUnavailable('Registered publication destination changed')
    return actual


_RAW_ENCODING_CHECKS = (_check_encoding_source, _check_encoding_destination)


def encoding_checks_are_current():
    return (_check_encoding_source, _check_encoding_destination) == _RAW_ENCODING_CHECKS


@dataclass
class RegisteredEncodingSource:
    stage: RegisteredEncodingStage
    path: str
    directory_fd: int
    source_fd: int
    version: tuple
    root_fd: int
    destination_fd: int
    destination_identity: tuple
    publisher: object
    active: bool = True
    published: str = None

    def check(self):
        if not self.active:
            raise SynthesisWorkspaceUnavailable('Registered publication source expired')
        # The caller already owns the final Project publication guard. This
        # metadata-only check must not reenter parent.admit's task/chunk mutexes.
        with self.stage._opened_directories() as fd:
            if _identity(os.fstat(fd)) != _identity(os.fstat(self.directory_fd)):
                raise SynthesisWorkspaceUnavailable('Registered publication directory changed')
            current = os.stat('voicelines', dir_fd=self.root_fd, follow_symlinks=False)
            if (tuple(_identity(os.fstat(self.root_fd))) != self.stage.root_identity
                    or not stat.S_ISDIR(current.st_mode)
                    or tuple(_identity(current)) != self.destination_identity
                    or tuple(_identity(os.fstat(self.destination_fd))) != self.destination_identity):
                raise SynthesisWorkspaceUnavailable('Registered destination directory changed')
            if self.published is None:
                _RAW_ENCODING_CHECKS[0](fd, Path(self.path).name, self.source_fd, self.version)
            else:
                _RAW_ENCODING_CHECKS[1](Path(self.published).name, self.source_fd, self.version, self.destination_fd)

    def publish(self, destination):
        from audio_generation_owner import ensure_audio_generation_publication
        from audio_validation import publish_audio_output
        with ensure_audio_generation_publication():
            if publish_audio_output is not self.publisher:
                raise SynthesisWorkspaceUnavailable('Registered publication writer changed')
            self.check()
            if Path(destination).parent != self.stage.directory.parent.parent / 'voicelines':
                raise SynthesisWorkspaceUnavailable('Registered destination escaped its captured book')
            os.fsync(self.source_fd)
            if not self.publisher(Path(self.path).name, destination,
                    source_dir_fd=self.directory_fd, source_fd=self.source_fd, source_version=self.version,
                    destination_dir_fd=self.destination_fd):
                from audio_generation_owner import AudioGenerationCancelled
                raise AudioGenerationCancelled()
            self.published = str(destination)
            self.check()


def get_registered_encoding_stage(raw_path):
    stage = _bound_encoding_stage(raw_path)
    if stage is not None:
        with stage._directories():
            pass
    return stage


def _bound_encoding_stage(raw_path):
    stages = _encoding_outputs.get()
    if stages is None:
        return None
    return stages.get(os.fspath(raw_path)) or stages.get(os.path.abspath(raw_path))


def defer_registered_encoding_cleanup(raw_path):
    # Even an unexpected output spelling cannot grant legacy unlink authority.
    # No live callback here: cancellation must leave the original registry owner
    # in charge without masking the original result or following mutable aliases.
    return _bound_encoding_stage(raw_path) is not None


def get_synthesis_workspace_parent(engine, category, parent, *, batch=False):
    """Return captured authority only for a known unmodified local writer."""
    from tts import (TTSEngine, _RAW_RECOVERY_METHODS, _RAW_BATCH_WORKSPACE_METHODS,
                     _RAW_CLONE_METHODS, _RAW_DESIGN_METHODS, _RAW_LORA_METHODS,
                     _RAW_ENSEMBLE_METHODS, _RAW_CLONE_BATCH_METHODS,
                     _RAW_LORA_BATCH_METHODS, _RAW_DESIGN_BATCH_METHODS,
                     _RAW_ENSEMBLE_BATCH_METHODS, _RAW_MIX_TO_UNISON, mix_to_unison,
                     UnsupportedVoiceBackendError)
    from synthesis_recovery_context import RecoveryParent, get_recovery_claim_digest
    single = {'custom': _RAW_RECOVERY_METHODS, 'clone': _RAW_CLONE_METHODS,
              'design': _RAW_DESIGN_METHODS, 'lora': _RAW_LORA_METHODS,
              'ensemble': _RAW_ENSEMBLE_METHODS}
    batch_methods = {'custom': _RAW_BATCH_WORKSPACE_METHODS,
                     'clone': _RAW_CLONE_BATCH_METHODS, 'lora': _RAW_LORA_BATCH_METHODS,
                     'design': _RAW_DESIGN_BATCH_METHODS,
                     'ensemble': _RAW_ENSEMBLE_BATCH_METHODS}
    if (type(engine) is not TTSEngine or category not in LOCAL_CATEGORIES
            or not is_synthesis_workspace_supported()
            or type(parent) is not RecoveryParent
            or get_recovery_claim_digest() != parent.claim_sha256):
        return None
    if category == 'ensemble' and mix_to_unison is not _RAW_MIX_TO_UNISON:
        return None
    # An ensemble may dispatch any ordinary member category. Conservatively
    # qualify all member writers before handing any of them a registered path.
    required = dict(single[category])
    if category == 'ensemble':
        for member_category in ('custom', 'clone', 'design', 'lora'):
            required.update(single[member_category])
    if batch:
        required.update(batch_methods[category])
    if not all(getattr(getattr(engine, name), '__func__', getattr(engine, name)) is method
               for name, method in required.items()):
        return None
    # Never invoke an overridden backend while deciding whether it is raw.
    try:
        backend = engine.get_voice_backend(category)
    except (UnsupportedVoiceBackendError, AttributeError, ValueError):
        return None
    return parent if backend == 'local' else None


@contextmanager
def ensure_synthesis_parent_output(parent, *, encoding=False):
    """Keep a raw Project parent registered until its caller finishes publication."""
    from audio_generation_owner import get_audio_generation_owner
    with ensure_synthesis_workspace(parent, 0, encoding=encoding) as directory:
        raw = str(directory / 'assembled-parent.wav')
        with parent.admit():
            with SynthesisWorkspaceRegistry(parent.project_root) as registry:
                descriptor = registry.create_encoded_parent(directory.name, '.wav')
                os.close(descriptor)
                row = registry.body['rows'][directory.name]
                stage = (RegisteredEncodingStage(parent, get_audio_generation_owner(), directory.resolve(strict=True),
                         tuple(_identity(os.fstat(registry.parent_fd))), tuple(_identity(os.fstat(registry.fd))),
                         tuple(row['identity']), tuple(row['join_identity']),
                         tuple(dict.fromkeys((raw, os.path.abspath(raw),
                             str(directory.resolve(strict=True) / 'assembled-parent.wav')))), _EncodingLifetime())
                         if encoding else None)
        token = None
        if stage is not None:
            bindings = dict(_encoding_outputs.get() or {})
            if (any(path in bindings for path in stage.raw_paths)
                    or len({id(value) for value in bindings.values()}) >= MAX_ROWS):
                stage.lifetime.active = False
                raise SynthesisWorkspaceUnavailable('Ambiguous or excessive encoding workspace bindings')
            bindings.update((path, stage) for path in stage.raw_paths)
            token = _encoding_outputs.set(MappingProxyType(bindings))
        try:
            yield raw
        finally:
            if stage is not None:
                stage.lifetime.active = False
                _encoding_outputs.reset(token)


@contextmanager
def ensure_synthesis_workspace(parent, windows, *, encoding=False):
    """Allocate only under current captured authority; cleanup can be deferred."""
    from audio_generation_owner import AudioGenerationOwner, get_audio_generation_owner
    from synthesis_recovery_context import RecoveryParent, get_recovery_claim_digest
    owner = get_audio_generation_owner()
    if (type(owner) is not AudioGenerationOwner or type(parent) is not RecoveryParent
            or get_recovery_claim_digest() != parent.claim_sha256):
        raise SynthesisWorkspaceUnavailable('Registered spill requires its captured live owner')
    with parent.admit():
        with SynthesisWorkspaceRegistry(parent.project_root, create=True) as registry:
            cleanup_root = Path(parent.project_root).resolve(strict=True)
            cleanup_identity = tuple(_identity(os.fstat(registry.parent_fd)))
            if tuple(_identity(os.stat(cleanup_root, follow_symlinks=False))) != cleanup_identity:
                raise SynthesisWorkspaceUnavailable('Workspace root changed during admission')
            name, row = registry.reserve(parent, windows, encoding=encoding)
    path = Path(parent.project_root) / DIRECTORY / name
    try:
        yield path
    finally:
        try:
            # Cancellation/input edits revoke publication, not ownership of the
            # already registered disposable scratch. Never replace an error or
            # a successful published parent with a cleanup failure.
            with owner(allow_cancelled=True):
                with SynthesisWorkspaceRegistry(cleanup_root, expected_root_identity=cleanup_identity) as registry:
                    registry.retire(name, row)
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            _LOG.warning('Deferred registered synthesis workspace cleanup: %s', error)


def apply_abandoned_synthesis_workspace_cleanup(root):
    """Caller holds global idle task admission then the canonical book lock."""
    if not is_synthesis_workspace_supported():
        return 0
    count = 0
    try:
        with SynthesisWorkspaceRegistry(root) as registry:
            names = tuple(registry.body['rows'])
    except FileNotFoundError:
        if os.path.lexists(Path(root) / DIRECTORY):
            _LOG.warning('Preserving synthesis workspace namespace with missing registry authority')
        return 0
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        _LOG.warning('Preserving unavailable synthesis workspace registry: %s', error)
        return 0
    for name in names:
        try:
            # Reopen after every attempt: failed unlink/sync/replace may leave
            # ambiguous disk state. A preserved unsafe row must not prevent
            # reclamation of later independently registered healthy workspaces.
            with SynthesisWorkspaceRegistry(root) as registry:
                count += registry.retire(name)
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            _LOG.warning('Preserving unreconciled synthesis workspace %s: %s', name, error)
    return count


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
