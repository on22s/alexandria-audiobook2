"""Pre-write logical byte limits for one private, seekable mux output.

The caller reserves this file's allowance under its stage lease. Native code
receives no file descriptor or real pathname; faststart may reopen only this
same output for reading. This is an I/O bound, not a process sandbox.
"""
import os
from contextlib import contextmanager
from pathlib import Path
import re
import stat
from threading import RLock

from audio_take_history import _ensure_plain_path, _open_regular


class ForwardOnlyMediaInput:
    """Expose a native producer pipe to PyAV without seek or close callbacks.

    Some pipe handles advertise seekability but reject the seeks demuxers use.
    The worker owns the underlying pipe and closes it after its child is reaped.
    """
    def __init__(self, stream):
        self._stream = stream

    def readable(self):
        return True

    def seekable(self):
        return False

    def read(self, size):
        return self._stream.read(size)


class BoundedMediaOutput:
    name = 'aa2-bounded://output'

    def __init__(self, root, filename, max_bytes, check_active=lambda: None):
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 2**63-1
                or not isinstance(filename, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', filename) is None):
            raise ValueError('Invalid bounded media output allowance or filename')
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise ValueError('Bounded output requires a canonical private directory')
        self.path = _ensure_plain_path(self.root/filename, self.root)
        self.limit, self.check_active = max_bytes, check_active
        self.failure, self.highwater, self._size = None, 0, 0
        self._lock, self._readers = RLock(), []
        self._file = self.path.open('xb+', buffering=0)
        info = os.fstat(self._file.fileno())
        self._identity = info.st_dev, info.st_ino

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            self.close()
        except OSError:
            if error_type is None:
                raise

    def _fail(self, message):
        self.failure = ValueError(message)
        raise self.failure

    def check(self):
        if self.failure is not None:
            raise self.failure
        try:
            self.check_active()
            _ensure_plain_path(self.path, self.root)
            current = self.path.lstat()
            opened = os.fstat(self._file.fileno())
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != self._identity
                    or (opened.st_dev, opened.st_ino) != self._identity
                    or current.st_size != self._size or opened.st_size != self._size):
                self._fail('Bounded media output changed outside its writer')
        except BaseException as error:
            self.failure = error
            raise

    @contextmanager
    def _operation(self):
        with self._lock:
            try:
                self.check()
                yield
            except BaseException as error:
                self.failure = error
                raise

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._file.tell()

    def _seek(self, stream, offset, whence):
        if type(offset) is not int or type(whence) is not int or whence not in (0, 1, 2):
            self._fail('Invalid bounded media seek')
        position = offset + (0 if whence == 0 else stream.tell() if whence == 1 else self._size)
        if not 0 <= position <= self.limit:
            self._fail('Media seek exceeds its output allowance')
        return stream.seek(position)

    def seek(self, offset, whence=os.SEEK_SET):
        with self._operation():
            return self._seek(self._file, offset, whence)

    def write(self, data):
        with self._operation():
            if not isinstance(data, bytes):
                self._fail('Mux output must contain bytes')
            end = self._file.tell() + len(data)
            if end > self.limit:
                self._fail('Media write exceeds its output allowance')
            try:
                count = self._file.write(data)
                if count != len(data):
                    self._fail('Incomplete bounded media write')
                self._size = max(self._size, end)
                self.highwater = max(self.highwater, self._size)
                return count
            except BaseException as error:
                self.failure = error
                raise

    def truncate(self, size=None):
        with self._operation():
            size = self._file.tell() if size is None else size
            if type(size) is not int or not 0 <= size <= self.limit:
                self._fail('Media truncate exceeds its output allowance')
            try:
                result = self._file.truncate(size)
                self._size = size
                self.highwater = max(self.highwater, size)
                return result
            except BaseException as error:
                self.failure = error
                raise

    def flush(self):
        with self._operation():
            self._file.flush()
            os.fsync(self._file.fileno())

    def reopen(self, url, flags, options):
        with self._operation():
            if url != self.name or type(flags) is not int or flags != 1 or type(options) is not dict or options:
                self._fail('Mux attempted an unapproved output reopen')
            stream, info = _open_regular(self.path, self.root, self.limit, allow_empty=True)
            if (info.st_dev, info.st_ino) != self._identity:
                stream.close()
                self._fail('Mux output changed before reopening')
            reader = _BoundedMediaReader(self, stream)
            self._readers.append(reader)
            return reader

    def close(self):
        # Closing remains possible after a latched error or native mux failure.
        with self._lock:
            failure = None
            for stream in [*self._readers, self._file]:
                try:
                    stream.close()
                except BaseException as error:
                    if failure is None:
                        failure = error
            if failure is not None:
                raise failure



class _BoundedMediaReader:
    def __init__(self, owner, stream):
        self.owner, self._file = owner, stream

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def tell(self):
        return self._file.tell()

    def seek(self, offset, whence=os.SEEK_SET):
        with self.owner._operation():
            return self.owner._seek(self._file, offset, whence)

    def read(self, size):
        with self.owner._operation():
            if type(size) is not int or not 0 <= size <= 2**31-1:
                self.owner._fail('Invalid bounded mux read')
            # AVIO callbacks may return short reads. Faststart can request a
            # whole large moov atom; serve it incrementally rather than allocate
            # that request or reject a valid reviewed cover/chapter table.
            return self._file.read(min(size,1024**2))

    def close(self):
        self._file.close()
