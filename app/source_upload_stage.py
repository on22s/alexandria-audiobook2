"""Descriptor-owned upload payloads; route integration is kept separate."""
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import threading
import weakref

_stages = weakref.WeakSet()
_stages_lock = threading.RLock()


def get_upload_stage_accounting(directory):
    """Return names to exclude and authoritative bytes owned by live streams."""
    root = Path(directory).resolve()
    with _stages_lock:
        active = [stage for stage in _stages
                  if stage.directory == root and stage._stream is not None]
        return ({stage._accounting_name for stage in active if stage._accounting_name is not None},
                sum(stage.size for stage in active))


class _RenameInfo(ctypes.Structure):
    _fields_ = [('replace', ctypes.c_ubyte), ('root', ctypes.c_void_p),
               ('length', ctypes.c_uint32), ('name', ctypes.c_wchar * 1)]


def _destination_name(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or name.startswith('.') or name.endswith((' ', '.'))
            or any(char in name for char in '/\\\0<>:"|?*')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError('Invalid source upload destination')
    stem = name.split('.')[0].casefold()
    if stem in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}:
        raise ValueError('Invalid source upload destination')
    return name


class OwnedSourceUploadStage:
    """Keep a bounded payload's descriptor until a native publication operation.

    POSIX payloads have no staging pathname. Windows keeps an owner handle
    denying data writes and renames; added links are checked before and after
    handle-based rename. This is not protection from arbitrary mutations of an
    already-public file after handoff. Unsupported filesystem primitives fail
    without a path-based copy/rename fallback.
    """
    def __init__(self, directory, *, byte_limit):
        if type(byte_limit) is not int or byte_limit < 0:
            raise ValueError('Invalid upload stage byte limit')
        self.byte_limit = byte_limit
        self._stream = None
        self._directory_fd = None
        self._guard = None
        self._api = None
        self._sealed = None
        self._size = 0
        self._digest = None
        self._published_path = None
        self._accounting_name = None
        self.publication_started = False
        named = Path(os.path.abspath(directory))
        before = named.lstat()
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ValueError('Upload storage must be a real directory')
        if os.name == 'nt':
            from direct_path_admission import is_plain_path
            if not is_plain_path(named):
                raise ValueError('Upload storage must not use reparse ancestors')
        self.directory = named.resolve(strict=True)
        current = self.directory.lstat()
        self._root_identity = (before.st_dev, before.st_ino)
        if (current.st_dev, current.st_ino) != self._root_identity:
            raise ValueError('Upload storage changed during admission')
        try:
            if os.name == 'nt':
                self._open_windows()
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                self._directory_fd = os.open(self.directory, flags)
                self._check_root()
                if sys.platform.startswith('linux'):
                    if not hasattr(os, 'O_TMPFILE'):
                        raise OSError(errno.ENOTSUP, 'Unnamed upload staging is unavailable')
                    descriptor = os.open(self.directory, os.O_RDWR | os.O_TMPFILE, 0o600)
                    try:
                        self._stream = os.fdopen(descriptor, 'w+b')
                    except BaseException:
                        os.close(descriptor)
                        raise
                elif sys.platform == 'darwin':
                    self._stream = tempfile.TemporaryFile(dir=self.directory)
                else:
                    raise OSError(errno.ENOTSUP, 'Descriptor upload publication is unavailable')
                if os.fstat(self._stream.fileno()).st_nlink != 0:
                    raise ValueError('Upload staging must be unnamed')
            with _stages_lock:
                _stages.add(self)
        except BaseException:
            self.close()
            raise

    def _open_windows(self):
        import msvcrt
        from windows_export_staging_directory import WindowsBookRoot, _api
        self._api = _api()
        self._guard = WindowsBookRoot(self.directory, self._root_identity)
        self._guard.__enter__()
        for _ in range(64):
            path = self.directory / ('.source-upload-' + secrets.token_hex(16) + '.part')
            handle = self._api.CreateFileW(str(path), 0xC0010080, 1, None, 1, 0x00200000, None)
            if handle != ctypes.c_void_p(-1).value:
                self._accounting_name = path.name
                break
            error = ctypes.get_last_error()
            if error not in (80, 183):
                raise ctypes.WinError(error)
        else:
            raise FileExistsError('Could not reserve an upload stage')
        descriptor = None
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            self._stream = os.fdopen(descriptor, 'w+b')
        except BaseException:
            try:
                delete = ctypes.c_ubyte(1)
                if not self._api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                else:
                    self._api.CloseHandle(handle)
            raise

    def _check_root(self):
        if self._guard is not None:
            self._guard.check()
        else:
            opened = os.fstat(self._directory_fd)
            current = self.directory.lstat()
            if (not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode)
                    or (opened.st_dev, opened.st_ino) != self._root_identity
                    or (current.st_dev, current.st_ino) != self._root_identity):
                raise ValueError('Upload storage changed during publication')

    @property
    def published_path(self):
        return self._published_path

    @property
    def size(self):
        return self._size

    def write(self, data):
        if self._stream is None or self._sealed is not None:
            raise ValueError('Upload stage is not writable')
        if not isinstance(data, bytes):
            raise TypeError('Upload stage accepts bytes')
        if len(data) > self.byte_limit - self._size:
            raise ValueError('Source upload exceeds its byte limit')
        self._stream.seek(0, os.SEEK_END)
        written = self._stream.write(data)
        if written != len(data):
            raise OSError('Incomplete upload stage write')
        self._size += written
        return written

    def read_bytes(self):
        if self._stream is None:
            raise ValueError('Upload stage is closed')
        self._stream.flush()
        self._stream.seek(0)
        raw = self._stream.read(self._size + 1)
        if len(raw) != self._size or len(raw) > self.byte_limit:
            raise ValueError('Upload stage extent changed')
        return raw

    def sha256(self):
        if self._digest is not None:
            return self._digest
        if self._sealed is None:
            self.seal()
        if self._stream is None:
            raise ValueError('Upload stage is closed')
        self._stream.seek(0)
        digest, length = hashlib.sha256(), 0
        while block := self._stream.read(1024 * 1024):
            length += len(block)
            if length > self.byte_limit:
                raise ValueError('Upload stage extent changed')
            digest.update(block)
        if length != self._size or self._version(os.fstat(self._stream.fileno())) != self._sealed:
            raise ValueError('Upload stage changed while hashing')
        self._digest = digest.hexdigest()
        return self._digest

    @staticmethod
    def _version(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def seal(self):
        if self._stream is None:
            raise ValueError('Upload stage is closed')
        self._stream.flush()
        os.fsync(self._stream.fileno())
        info = os.fstat(self._stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size != self._size
                or info.st_size > self.byte_limit or info.st_nlink != (1 if os.name == 'nt' else 0)):
            raise ValueError('Upload stage changed before publication')
        self._sealed = self._version(info)
        self._check_root()

    def _rename_windows(self, destination):
        import msvcrt
        name = str(destination).encode('utf-16-le')
        buffer = ctypes.create_string_buffer(_RenameInfo.name.offset + len(name) + 2)
        info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
        info.replace, info.root, info.length = 0, None, len(name)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, name, len(name))
        handle = msvcrt.get_osfhandle(self._stream.fileno())
        if not self._api.SetFileInformationByHandle(handle, 3, buffer, len(buffer)):
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise FileExistsError(error, 'Upload destination exists')
            raise ctypes.WinError(error)

    def publish(self, name):
        name = _destination_name(name)
        if self._sealed is None:
            self.seal()
        if self._stream is None or self._published_path is not None:
            raise ValueError('Upload stage is not available for publication')
        self._check_root()
        info = os.fstat(self._stream.fileno())
        if (self._version(info) != self._sealed
                or info.st_nlink != (1 if os.name == 'nt' else 0)):
            raise ValueError('Upload stage changed after validation')
        destination = self.directory / name
        if os.name == 'nt':
            self._rename_windows(destination)
            self._accounting_name = name
            self.publication_started = True
            if os.fstat(self._stream.fileno()).st_nlink != 1:
                self.close()  # exact handle deletes only its unaccepted link
                raise ValueError('Upload stage acquired an alias during publication')
        elif sys.platform.startswith('linux'):
            os.link('/proc/self/fd/' + str(self._stream.fileno()), name,
                    dst_dir_fd=self._directory_fd, follow_symlinks=True)
            self.publication_started = True
        else:
            library = ctypes.CDLL(None, use_errno=True)
            clone = getattr(library, 'fclonefileat', None)
            if clone is None:
                raise OSError(errno.ENOTSUP, 'Descriptor upload cloning is unavailable')
            clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            clone.restype = ctypes.c_int
            if clone(self._stream.fileno(), self._directory_fd, os.fsencode(name), 0) != 0:
                raise OSError(ctypes.get_errno(), 'Descriptor upload cloning failed')
            self.publication_started = True
        self._accounting_name = name
        self._published_path = str(destination)
        self._close_stream()
        return self._published_path

    def sync_directory(self):
        self._check_root()
        if self._directory_fd is not None:
            os.fsync(self._directory_fd)
        else:
            from adapter_publication import sync_adapter_directory
            sync_adapter_directory(str(self.directory))

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                if self._stream.closed:
                    self._stream = None

    def close(self):
        try:
            if os.name == 'nt' and self._stream is not None and self._published_path is None:
                import msvcrt
                delete = ctypes.c_ubyte(1)
                handle = msvcrt.get_osfhandle(self._stream.fileno())
                if not self._api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), 1):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            try:
                self._close_stream()
            finally:
                try:
                    if self._directory_fd is not None:
                        descriptor, self._directory_fd = self._directory_fd, None
                        os.close(descriptor)
                finally:
                    if self._guard is not None:
                        guard, self._guard = self._guard, None
                        guard.__exit__(None, None, None)
                    if self._stream is None:
                        with _stages_lock:
                            _stages.discard(self)
