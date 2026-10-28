"""Native Windows staging cleanup prototype; native qualification is pending.

Directory handles deny rename while child paths are in use. Deletion addresses
the opened object through FileDispositionInfo, including empty directories.
"""
from contextlib import contextmanager, ExitStack
import ctypes
import os
import stat
from pathlib import Path

from export_staging_directory import StageDirectoryChanged, OWNER_FILE
from audio_take_history import _version


class _FileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]


class _FileInformation(ctypes.Structure):
    _fields_ = [('attributes', ctypes.c_uint32), ('created', _FileTime),
                ('accessed', _FileTime), ('written', _FileTime),
                ('volume', ctypes.c_uint32), ('size_high', ctypes.c_uint32),
                ('size_low', ctypes.c_uint32), ('links', ctypes.c_uint32),
                ('index_high', ctypes.c_uint32), ('index_low', ctypes.c_uint32)]


def _api():
    if os.name != 'nt':
        raise StageDirectoryChanged('Native Windows staging cleanup is unavailable')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateFileW': ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                         ctypes.c_void_p], ctypes.c_void_p),
        'GetFileInformationByHandle': ([ctypes.c_void_p, ctypes.POINTER(_FileInformation)], ctypes.c_int),
        'SetFileInformationByHandle': ([ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        'CloseHandle': ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        operation = getattr(api, name)
        operation.argtypes, operation.restype = arguments, result
    return api


class _Handle:
    def __init__(self, api, path, *, delete=False, create=False, protect_rename=False):
        self.api = api
        if type(protect_rename) is not bool or delete and protect_rename:
            raise ValueError('Invalid Windows handle protection mode')
        # A deletion handle permits existing readers, but denies new writers
        # and renames while the object's final version is checked and removed.
        # A rename guard requests DELETE access but keeps read/write sharing so
        # the guarded directory's children can still be published normally.
        share_mode = 1 if delete else 3
        delete_access = delete or protect_rename
        self.handle = api.CreateFileW(str(path), 0x80 | (0x10000 if delete_access else 0),
                                      share_mode, None, 1 if create else 3,
                                      0x02000000 | 0x00200000, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def information(self):
        info = _FileInformation()
        if not self.api.GetFileInformationByHandle(self.handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info


    def delete(self):
        delete = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(self.handle, 4, ctypes.byref(delete), 1):
            raise ctypes.WinError(ctypes.get_last_error())


def _identity(info):
    return info.volume, (info.index_high << 32) | info.index_low


def _open_rename_guard(api, directory, *, required):
    """Request rename authority where the app controls the directory tree.

    System ancestors can deny DELETE access to an ordinary user precisely
    because that user cannot rename them.  Retain an identity handle there;
    never weaken the guard for the admitted root or one of its descendants.
    """
    try:
        return _Handle(api, directory, protect_rename=True)
    except OSError as error:
        # Access denial means this token cannot rename the system ancestor; a
        # sharing violation means another live handle already denies it.  Both
        # are acceptable only outside the app-controlled root.
        if required or getattr(error, 'winerror', None) not in (5, 32):
            raise
        return _Handle(api, directory)


class WindowsBookRoot:
    """Pin one admitted book root and its ancestor chain without blocking writes.

    Unlike ``WindowsStageDirectory``, this guard never deletes the opened root
    and therefore does not deny write sharing.  It prevents the root or an
    ancestor from being renamed while a transaction creates journals and moves
    child artifacts inside the admitted directory.
    """
    def __init__(self, path, expected_identity):
        if (type(expected_identity) is not tuple or len(expected_identity) != 2
                or any(type(value) is not int or value < 0 for value in expected_identity)):
            raise ValueError('Invalid admitted book root identity')
        self.path = Path(os.path.abspath(path))
        self.expected = expected_identity
        self.stack = ExitStack()
        self.guards = []
        self.entered = False

    def __enter__(self):
        if self.entered:
            raise StageDirectoryChanged('Book root context cannot be reused')
        self.entered = True
        try:
            api = _api()
            for directory in (*reversed(self.path.parents), self.path):
                handle = _open_rename_guard(
                    api, directory, required=directory == self.path)
                self.stack.callback(handle.close)
                info = handle.information()
                if not info.attributes & 0x10 or info.attributes & 0x400:
                    raise StageDirectoryChanged('Unsafe book root ancestor')
                current = directory.lstat()
                identity = _identity(info)
                if (current.st_dev, current.st_ino) != identity:
                    raise StageDirectoryChanged('Book root ancestor was replaced')
                self.guards.append((directory, handle, identity))
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        try:
            self.stack.close()
        finally:
            self.guards = []

    def check(self):
        if not self.guards:
            raise StageDirectoryChanged('Book root context is not active')
        for directory, handle, expected in self.guards:
            info = handle.information()
            current = directory.lstat()
            if (not info.attributes & 0x10 or info.attributes & 0x400
                    or _identity(info) != expected
                    or (current.st_dev, current.st_ino) != expected):
                raise StageDirectoryChanged('Book root ancestor changed')
        if self.guards[-1][2] != self.expected:
            raise StageDirectoryChanged('Admitted book root was replaced')


@contextmanager
def guard_windows_book_move_parents(root, source, destination):
    """Pin every parent without delete sharing while an admitted move runs.

    Reparse points and paths outside the book root are refused. The guard does
    not authorize source bytes, publication, staging cleanup, or crash recovery.
    """
    root = Path(os.path.abspath(root))
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    for path in (source, destination):
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise ValueError('Book move escapes its admitted root') from error
        if not relative.parts:
            raise ValueError('Book move requires a child path')
    paths = {}
    for path in (source.parent, destination.parent):
        for directory in (*path.parents, path):
            key = os.path.normcase(os.path.abspath(directory))
            paths[key] = directory
    ordered = sorted(paths.values(), key=lambda path: (len(path.parts), os.path.normcase(str(path))))
    api = _api()
    with ExitStack() as stack:
        guards = []
        for directory in ordered:
            handle = _open_rename_guard(
                api, directory,
                required=(directory == root or directory.is_relative_to(root)))
            stack.callback(handle.close)
            info = handle.information()
            if not info.attributes & 0x10 or info.attributes & 0x400:
                raise StageDirectoryChanged('Unsafe book move ancestor')
            current = directory.lstat()
            if (current.st_dev, current.st_ino) != _identity(info):
                raise StageDirectoryChanged('Book move ancestor was replaced')
            guards.append((directory, handle, _identity(info)))
        yield
        for directory, handle, expected in guards:
            info = handle.information()
            current = directory.lstat()
            if (not info.attributes & 0x10 or info.attributes & 0x400
                    or _identity(info) != expected or (current.st_dev, current.st_ino) != expected):
                raise StageDirectoryChanged('Book move ancestor changed')


class WindowsStageDirectory:
    def __init__(self, path, expected_identity):
        if (type(expected_identity) is not tuple or len(expected_identity) != 2
                or any(type(value) is not int or value < 0 for value in expected_identity)):
            raise ValueError('Invalid admitted staging identity')
        path = Path(os.path.abspath(path))
        # Preserve the caller's actual ancestor chain. Resolving the parent here
        # would erase the very junction/reparse component that __enter__ must
        # reject before cleanup or staging uses it.
        self.path = path
        self.expected = expected_identity
        self.stack = ExitStack()
        self.stage = None
        self.entered = False

    def _hold(self, path, *, delete=False):
        handle = _Handle(self.api, path, delete=delete)
        self.stack.callback(handle.close)
        return handle

    def __enter__(self):
        if self.entered:
            raise StageDirectoryChanged('Staging directory context cannot be reused')
        self.entered = True
        try:
            self.api = _api()
            # Hold every ancestor: denying rename only on the leaf does not
            # prevent its parent pathname from being redirected.
            for parent in reversed(self.path.parents):
                info = self._hold(parent).information()
                if not info.attributes & 0x10 or info.attributes & 0x400:
                    raise StageDirectoryChanged('Unsafe staging ancestor')
            self.stage = self._hold(self.path, delete=True)
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        try:
            self.stack.close()
        finally:
            self.stage = None

    def check(self):
        if self.stage is None:
            raise StageDirectoryChanged('Staging directory context is not active')
        info = self.stage.information()
        if (not info.attributes & 0x10 or info.attributes & 0x400
                or _identity(info) != self.expected):
            raise StageDirectoryChanged('Admitted staging directory was replaced')

    def remove_contents(self, retained_names):
        if (type(retained_names) is not frozenset
                or any(not _member(name) for name in retained_names)):
            raise ValueError('Invalid retained staging names')
        self.check()
        if self.has_recovery_guard():
            raise StageDirectoryChanged('Staging recovery guard requires explicit recovery')
        self._remove_children(self.path, retained_names, 0)

    def open_owner_stream(self, *, create=False):
        self.check()
        guard = _Handle(self.api, self.path / OWNER_FILE, create=create)
        stream = None
        try:
            info = guard.information()
            if info.attributes & (0x10 | 0x400) or info.links != 1:
                raise StageDirectoryChanged('Unsafe staging owner lock')
            stream = (self.path / OWNER_FILE).open('r+b')
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != _identity(info):
                raise StageDirectoryChanged('Staging owner lock was replaced')
            self.check_owner_stream(stream)
            return stream
        except BaseException:
            if stream is not None:
                stream.close()
            raise
        finally:
            try:
                guard.close()
            except BaseException:
                if stream is not None:
                    stream.close()
                raise

    def read_marker_bytes(self, limit):
        if type(limit) is not int or not 0 < limit <= 4096:
            raise ValueError('Invalid staging marker read limit')
        self.check()
        path = self.path / '.export-staging.json'
        guard = _Handle(self.api, path)
        try:
            info = guard.information()
            if info.attributes & (0x10 | 0x400) or info.links != 1:
                raise StageDirectoryChanged('Unsafe staging marker')
            with path.open('rb') as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                        or (before.st_dev, before.st_ino) != _identity(info)):
                    raise StageDirectoryChanged('Staging marker identity changed')
                raw = stream.read(limit + 1)
                if (len(raw) > limit or _version(os.fstat(stream.fileno())) != _version(before)
                        or _version(path.lstat()) != _version(before)):
                    raise StageDirectoryChanged('Staging marker changed while reading')
            self.check()
            return raw
        finally:
            guard.close()

    def write_marker_bytes(self, raw):
        if type(raw) is not bytes or not 0 < len(raw) <= 4096:
            raise ValueError('Invalid staging marker bytes')
        self.check()
        path = self.path / '.export-staging.json'
        with path.open('xb') as stream:
            if stream.write(raw) != len(raw):
                raise OSError('Short staging marker write')
            stream.flush()
            os.fsync(stream.fileno())
            opened = os.fstat(stream.fileno())
            if opened.st_nlink != 1 or _version(opened) != _version(path.lstat()):
                raise StageDirectoryChanged('Staging marker replaced during creation')
        self.check()

    def check_owner_stream(self, stream):
        self.check()
        opened, named = os.fstat(stream.fileno()), (self.path / OWNER_FILE).lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > 4096
                or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or getattr(named, 'st_file_attributes', 0) & 0x400
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            raise StageDirectoryChanged('Unsafe or replaced staging owner lock')

    def _remove_children(self, path, retained_names, depth):
        if depth > 64:
            raise StageDirectoryChanged('Staging cleanup nesting limit exceeded')
        if depth:
            # A nested stage has an independent owner. Its Windows retirement
            # protocol is not qualified here, so preserve it before touching
            # either metadata or payload, including when the marker is torn.
            for name in ('.export-staging.json', OWNER_FILE, '.export-staging-recovery'):
                try:
                    (path / name).lstat()
                except FileNotFoundError:
                    continue
                raise StageDirectoryChanged('Nested staging owner requires explicit recovery')
        from export_staging_retirement import DIRECTORY
        try:
            (path / DIRECTORY).lstat()
        except FileNotFoundError:
            pass
        else:
            raise StageDirectoryChanged('Nested export registry requires explicit recovery')
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name in retained_names:
                    continue
                if entry.name == DIRECTORY:
                    raise StageDirectoryChanged('Nested export registry requires explicit recovery')
                self.check()
                before = os.stat(path / entry.name, follow_symlinks=False)
                child = _Handle(self.api, path / entry.name, delete=True)
                try:
                    info = child.information()
                    if _identity(info) != (before.st_dev, before.st_ino):
                        raise StageDirectoryChanged('Staging child was replaced')
                    if info.attributes & 0x10 and not info.attributes & 0x400:
                        self._remove_children(path / entry.name, frozenset(), depth + 1)
                    child.delete()
                finally:
                    child.close()

    def unlink(self, name):
        if not _member(name):
            raise ValueError('Invalid staging member')
        self.check()
        if self.has_recovery_guard():
            raise StageDirectoryChanged('Staging recovery guard requires explicit recovery')
        self._unlink_file(name)

    def retire_marker_for_recovery(self, name):
        """Retire only the public marker after preservation is durable.

        Ordinary deletion remains forbidden once a recovery guard exists.  A
        preserved stage must nevertheless lose its public marker so scanners
        cannot mistake it for abandoned, reclaimable output.  A missing guard
        fails closed, and a concurrent guard removal still leaves the missing
        public marker as an independent preservation signal.
        """
        if name != '.export-staging.json':
            raise ValueError('Invalid staging recovery marker')
        self.check()
        if not self.has_recovery_guard():
            raise StageDirectoryChanged('Staging recovery guard is missing')
        self._unlink_file(name)

    def _unlink_file(self, name):
        child = _Handle(self.api, self.path / name, delete=True)
        try:
            info = child.information()
            if info.attributes & 0x10:
                raise StageDirectoryChanged('Staging metadata is a directory')
            child.delete()
        finally:
            child.close()

    def has_rollback_backup(self):
        self.check()
        try:
            guard = _Handle(self.api, self.path / 'old')
        except FileNotFoundError:
            return False
        try:
            info = guard.information()
            if not info.attributes & 0x10 or info.attributes & 0x400:
                return True
            with os.scandir(self.path / 'old') as entries:
                populated = next(entries, None) is not None
            self.check()
            return populated
        finally:
            guard.close()

    def remove_empty_directory(self):
        self.check()
        if self.has_recovery_guard():
            raise StageDirectoryChanged('Staging recovery guard requires explicit recovery')
        self.stage.delete()

    def has_recovery_guard(self):
        self.check()
        try:
            (self.path / '.export-staging-recovery').lstat()
        except FileNotFoundError:
            return False
        return True

    def create_recovery_guard(self, name):
        if name != '.export-staging-recovery':
            raise ValueError('Invalid staging recovery guard')
        self.check()
        with (self.path / name).open('xb') as stream:
            stream.write(b'1')


def _member(name):
    return (type(name) is str and bool(name) and name not in ('.', '..')
            and not any(character in name for character in ('/', '\\', '\x00', ':')))
