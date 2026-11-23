"""Directory-bound export cleanup used by creation and recovery.

The caller must supply the directory identity captured at creation/admission.
This POSIX implementation is one part of the cross-platform cleanup repair.
"""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import stat
from utils import file_lock
from audio_take_history import _version

OWNER_FILE = '.export-staging.owner.lock'
RECOVERY_FILE = '.export-staging-recovery'


@contextmanager
def stage_owner_lock(authority, *, create=False):
    """Lock an admitted regular file without reopening an unchecked pathname."""
    with authority.open_owner_stream(create=create) as stream:
        with file_lock(authority.path / '.export-staging.owner', timeout=0, lock_handle=stream):
            authority.check_owner_stream(stream)
            yield stream


def identity(info):
    return info.st_dev, info.st_ino


class StageDirectoryChanged(OSError):
    pass


def stage_directory(path, expected_identity):
    if os.name == 'nt':
        from windows_export_staging_directory import WindowsStageDirectory
        return WindowsStageDirectory(path, expected_identity)
    return PosixStageDirectory(path, expected_identity)


class PosixStageDirectory:
    def __init__(self, path, expected_identity):
        if (type(expected_identity) is not tuple or len(expected_identity) != 2
                or any(type(value) is not int or value < 0 for value in expected_identity)):
            raise ValueError('Invalid admitted staging identity')
        self.path = Path(path)
        self.expected = expected_identity
        self.parent_fd = self.fd = None
        self.stack = ExitStack()
        self.entered = False

    def __enter__(self):
        if self.entered:
            raise StageDirectoryChanged('Staging directory context cannot be reused')
        self.entered = True
        try:
            if (not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW')
                    or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
                    or os.unlink not in os.supports_dir_fd or os.rmdir not in os.supports_dir_fd
                    or os.scandir not in os.supports_fd):
                raise StageDirectoryChanged('Pinned staging cleanup is unavailable')
            self.parent_fd = os.open(self.path.parent.resolve(strict=True),
                                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.stack.callback(os.close, self.parent_fd)
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.parent_fd)
            self.stack.callback(os.close, self.fd)
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        try:
            self.stack.close()
        finally:
            self.fd = self.parent_fd = None

    def check(self):
        if self.fd is None:
            raise StageDirectoryChanged('Staging directory context is not active')
        named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(named.st_mode) or identity(named) != self.expected
                or identity(os.fstat(self.fd)) != self.expected):
            raise StageDirectoryChanged('Admitted staging directory was replaced')

    def remove_contents(self, retained_names):
        """Delete through the admitted descriptor; never follow child aliases."""
        if (type(retained_names) is not frozenset
                or any(type(name) is not str or not name or name in ('.', '..')
                       or '/' in name or '\\' in name for name in retained_names)):
            raise ValueError('Invalid retained staging names')
        self.check()
        self._remove_children(self.fd, retained_names, 0)

    def open_owner_stream(self, *, create=False):
        self.check()
        descriptor = os.open(OWNER_FILE, os.O_RDWR | ((os.O_CREAT | os.O_EXCL) if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=self.fd)
        stream = os.fdopen(descriptor, 'r+b')
        try:
            self.check_owner_stream(stream)
            return stream
        except BaseException:
            stream.close()
            raise

    def read_marker_bytes(self, limit):
        return self._read_metadata('.export-staging.json', limit)[0]

    def _read_metadata(self, name, limit):
        if type(limit) is not int or not 0 < limit <= 4096:
            raise ValueError('Invalid staging marker read limit')
        self.check()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > limit):
                raise StageDirectoryChanged('Unsafe staging marker')
            if (name == RECOVERY_FILE and before.st_size == 0
                    and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & ~0o600)):
                raise StageDirectoryChanged('Unsafe interrupted staging recovery guard')
            raw = stream.read(limit + 1)
            if (len(raw) > limit or _version(os.fstat(stream.fileno())) != _version(before)
                    or _version(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) != _version(before)):
                raise StageDirectoryChanged('Staging marker changed while reading')
        self.check()
        return raw, _version(before)

    def read_recovery_guard(self, *, allow_empty=False):
        raw, version = self._read_metadata(RECOVERY_FILE, 1)
        if raw != b'1' and (not allow_empty or raw != b''):
            raise StageDirectoryChanged('Unknown staging recovery guard')
        return version

    def unlink_recovery_guard(self, expected):
        if self.read_recovery_guard(allow_empty=expected[2] == 0) != expected:
            raise StageDirectoryChanged('Staging recovery guard changed')
        self.unlink(RECOVERY_FILE)

    def write_marker_bytes(self, raw):
        if type(raw) is not bytes or not 0 < len(raw) <= 4096:
            raise ValueError('Invalid staging marker bytes')
        self.check()
        name = '.export-staging.json'
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        with os.fdopen(descriptor, 'wb') as stream:
            if stream.write(raw) != len(raw):
                raise OSError('Short staging marker write')
            stream.flush()
            os.fsync(stream.fileno())
            opened = os.fstat(stream.fileno())
            named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if opened.st_nlink != 1 or _version(opened) != _version(named):
                raise StageDirectoryChanged('Staging marker replaced during creation')
        os.fsync(self.fd)
        self.check()

    def check_owner_stream(self, stream):
        self.check()
        opened = os.fstat(stream.fileno())
        named = os.stat(OWNER_FILE, dir_fd=self.fd, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > 4096
                or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or identity(opened) != identity(named)):
            raise StageDirectoryChanged('Unsafe or replaced staging owner lock')

    def unlink(self, name):
        if (type(name) is not str or not name or name in ('.', '..')
                or '/' in name or '\\' in name):
            raise ValueError('Invalid staging member')
        self.check()
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def has_recovery_guard(self):
        self.check()
        try:
            os.stat(RECOVERY_FILE, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def has_rollback_backup(self):
        """Inspect rollback contents without following an untrusted old alias."""
        self.check()
        try:
            info = os.stat('old', dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            # An unexpected node at the rollback name is not cleanup authority.
            return True
        descriptor = os.open('old', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=self.fd)
        try:
            if identity(os.fstat(descriptor)) != identity(info):
                raise StageDirectoryChanged('Rollback directory was replaced')
            with os.scandir(descriptor) as entries:
                populated = next(entries, None) is not None
            current = os.stat('old', dir_fd=self.fd, follow_symlinks=False)
            if identity(current) != identity(info):
                raise StageDirectoryChanged('Rollback directory was replaced')
            self.check()
            return populated
        finally:
            os.close(descriptor)

    def remove_empty_directory(self):
        # POSIX rmdir cannot atomically condition removal on an inode. Callers
        # exclude name mutations: production retirement uses a private 0700
        # slot and retains its registry lock through this final operation.
        self.check()
        os.rmdir(self.path.name, dir_fd=self.parent_fd)

    def create_recovery_guard(self, name):
        if name != RECOVERY_FILE:
            raise ValueError('Invalid staging recovery guard')
        self.check()
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        try:
            if os.write(descriptor, b'1') != 1:
                raise OSError('Short staging recovery guard write')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self.fd)

    def _remove_children(self, directory_fd, retained_names, depth):
        if depth > 64:
            raise StageDirectoryChanged('Staging cleanup nesting limit exceeded')
        # A nested registry is an independent lock domain, not an ordinary
        # payload directory. Generic recursion cannot establish that its owners
        # have stopped, even when this outer staging owner is admitted.
        from export_staging_retirement import DIRECTORY
        try:
            os.stat(DIRECTORY, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StageDirectoryChanged('Nested export registry requires explicit recovery')
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if entry.name in retained_names:
                    continue
                if entry.name == DIRECTORY:
                    raise StageDirectoryChanged('Nested export registry requires explicit recovery')
                self.check()
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    try:
                        if identity(os.fstat(child_fd)) != identity(info):
                            raise StageDirectoryChanged('Staging child directory was replaced')
                        for protected in ('.export-staging.json', OWNER_FILE):
                            try:
                                os.stat(protected, dir_fd=child_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            raise StageDirectoryChanged('Nested staging owner requires explicit recovery')
                        self._remove_children(child_fd, frozenset(), depth + 1)
                        current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                        if identity(current) != identity(info) or not stat.S_ISDIR(current.st_mode):
                            raise StageDirectoryChanged('Staging child directory was replaced')
                        os.rmdir(entry.name, dir_fd=directory_fd)
                    finally:
                        os.close(child_fd)
                else:
                    os.unlink(entry.name, dir_fd=directory_fd)
