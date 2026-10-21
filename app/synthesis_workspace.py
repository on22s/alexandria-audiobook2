"""Registered disposable synthesis spill, separate from reusable audio evidence.

Only exact live Project admission creates workspaces. Abandoned reclamation is
called under the existing global task-idle barrier and canonical book lock.
Unsupported/unleased callers retain their ordinary temporary-file lifetime.
"""
from contextlib import contextmanager
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

from utils import file_lock

DIRECTORY = '.synthesis-workspaces-v1'
INDEX = 'registry.json'
PENDING = 'registry.pending'
MAX_ROWS = 256
MAX_WINDOWS = 1024
MAX_INDEX_BYTES = 1024 * 1024
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
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
                or type(row) is not dict or set(row) != {'claim', 'book', 'windows', 'identity', 'join_identity'}
                or any(not isinstance(row[key], str) or not _HEX64.fullmatch(row[key]) for key in ('claim', 'book'))
                or type(row['windows']) is not int or not 2 <= row['windows'] <= MAX_WINDOWS):
            raise SynthesisWorkspaceUnavailable('Invalid workspace row')
        for key in ('identity', 'join_identity'):
            value = row[key]
            if value is not None and (type(value) is not list or len(value) != 2
                    or any(type(item) is not int or item < 0 for item in value)):
                raise SynthesisWorkspaceUnavailable('Invalid workspace directory identity')
        if (row['identity'] is None) != (row['join_identity'] is None):
            raise SynthesisWorkspaceUnavailable('Incomplete workspace creation identity')


class SynthesisWorkspaceRegistry:
    """One locked registry transaction; caller acquires live/idle authority first.

    No owner callbacks or task admission while this lock is held. All data IO is
    relative to pinned directories. A malformed existing authority is preserved.
    """
    def __init__(self, root, *, create=False):
        self.root = Path(root)
        self.create = create
        self.parent_fd = self.fd = None
        self._lock_stream = self._lock_context = None
        self.body = None

    def __enter__(self):
        try:
            self.parent_fd = os.open(self.root.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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

    def reserve(self, parent, windows):
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

    def _delete_contents(self, name, row):
        workspace_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        join_fd = None
        try:
            if _identity(os.fstat(workspace_fd)) != row['identity']:
                raise SynthesisWorkspaceUnavailable('Workspace directory changed')
            allowed = {f'window-{index}.wav' for index in range(row['windows'])} | {'assembled-parent.wav', 'join'}
            entries = self._get_names(workspace_fd, len(allowed))
            if not set(entries) <= allowed:
                raise SynthesisWorkspaceUnavailable('Unknown workspace contents preserved')
            if 'join' in entries:
                info = os.stat('join', dir_fd=workspace_fd, follow_symlinks=False)
                self._check_directory(info)
                if _identity(info) != row['join_identity']:
                    raise SynthesisWorkspaceUnavailable('Join directory changed')
                join_fd = os.open('join', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
                if _identity(os.fstat(join_fd)) != row['join_identity']:
                    raise SynthesisWorkspaceUnavailable('Join directory changed')
                joined = self._get_names(join_fd, 1)
                if not set(joined) <= {'complete.wav'}:
                    raise SynthesisWorkspaceUnavailable('Unknown join contents preserved')
                for child in joined:
                    self._check_file(join_fd, child)
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


@contextmanager
def ensure_synthesis_workspace(parent, windows):
    """Allocate only under current captured authority; cleanup can be deferred."""
    from audio_generation_owner import AudioGenerationOwner, get_audio_generation_owner
    from synthesis_recovery_context import RecoveryParent, get_recovery_claim_digest
    owner = get_audio_generation_owner()
    if (type(owner) is not AudioGenerationOwner or type(parent) is not RecoveryParent
            or get_recovery_claim_digest() != parent.claim_sha256):
        raise SynthesisWorkspaceUnavailable('Registered spill requires its captured live owner')
    with parent.admit():
        with SynthesisWorkspaceRegistry(parent.project_root, create=True) as registry:
            name, row = registry.reserve(parent, windows)
    path = Path(parent.project_root) / DIRECTORY / name
    try:
        yield path
    finally:
        try:
            # Cancellation/input edits revoke publication, not ownership of the
            # already registered disposable scratch. Never replace an error or
            # a successful published parent with a cleanup failure.
            with owner(allow_cancelled=True):
                with SynthesisWorkspaceRegistry(parent.project_root) as registry:
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
