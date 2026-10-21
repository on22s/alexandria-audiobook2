"""One close-owned CPU ASR lease and finite, restart-reclaimable private scratch."""
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys

from word_timing import MAX_AUDIO_BYTES, run_timing_command

DIRECTORY = '.alexandria-cpu-asr-v1'
AUTHORITY = b'Alexandria CPU ASR scratch version 1\n'
OUTPUTS = ('input.audio', 'audio.wav', 'asr.json')


class CPUASRUnavailable(ValueError):
    """No safe provider slot or known scratch authority is available."""


def _identity(info):
    return info.st_dev, info.st_ino


def is_close_owned_cpu_asr_supported():
    return sys.platform in ('linux', 'darwin')


def get_cpu_asr_scratch_parent():
    return Path.home()


class CPUASRScratch:
    """Hold across snapshot, decode and both providers; never acquire book/GPU locks.

    Only close the lease: outside subprocess owners retain the same open-file
    description until descendants are reaped. On an exception leave bounded
    known scratch for the next admitted owner, rather than racing a late child.
    ``parent`` exists for isolated fixtures; production uses one per-user path.
    """
    def __init__(self, parent=None):
        self.parent = get_cpu_asr_scratch_parent() if parent is None else Path(parent)
        self.path = self.parent / DIRECTORY
        self.parent_fd = self.fd = self.lease_fd = None
        self.admitted = False

    def __enter__(self):
        if not is_close_owned_cpu_asr_supported():
            raise CPUASRUnavailable('Close-owned CPU ASR scratch is unavailable on this platform.')
        try:
            self.parent = self.parent.resolve(strict=True)
            self.path = self.parent / DIRECTORY
            self.parent_fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            created = False
            try:
                os.mkdir(DIRECTORY, 0o700, dir_fd=self.parent_fd)
                created = True
                os.fsync(self.parent_fd)
            except FileExistsError:
                pass
            self.fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self._check_root()
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if created else 0)
            self.lease_fd = self._open('lease', flags)
            try:
                import fcntl
                fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise CPUASRUnavailable('CPU ASR is busy; retry after the current check finishes.') from error
                raise
            if created:
                with os.fdopen(self._open('authority', os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as stream:
                    stream.write(AUTHORITY)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(self.fd)
            self._validate_layout()
            self.admitted = True
            self._cleanup()
            return self
        except BaseException:
            self._close()
            raise

    def _check_root(self):
        current = os.stat(DIRECTORY, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid()
                or current.st_mode & 0o077 or _identity(current) != _identity(os.fstat(self.fd))
                or _identity(current) != _identity(self.path.lstat())):
            raise CPUASRUnavailable('CPU ASR scratch directory changed or is not private.')
        if self.lease_fd is not None:
            info = os.stat('lease', dir_fd=self.fd, follow_symlinks=False)
            self._check_file(info)
            if _identity(info) != _identity(os.fstat(self.lease_fd)):
                raise CPUASRUnavailable('CPU ASR lease identity changed.')

    def __fspath__(self):
        return os.fspath(self.path)

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise CPUASRUnavailable('CPU ASR scratch contains an unsafe file; preserve it for inspection.')

    def _open(self, name, flags):
        self._check_root()
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            self._check_file(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_layout(self):
        self._check_root()
        if set(os.listdir(self.fd)) - {'lease', 'authority', *OUTPUTS}:
            raise CPUASRUnavailable('CPU ASR scratch contains unknown files; preserve it for inspection.')
        for name in os.listdir(self.fd):
            self._check_file(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
        if os.fstat(self.lease_fd).st_size > 1:
            raise CPUASRUnavailable('CPU ASR lease is malformed.')
        with os.fdopen(self._open('authority', os.O_RDONLY), 'rb') as stream:
            if stream.read(len(AUTHORITY) + 1) != AUTHORITY:
                raise CPUASRUnavailable('CPU ASR scratch authority is malformed; preserve it for inspection.')

    def _cleanup(self):
        self._validate_layout()  # Validate all names before deleting any of them.
        for name in OUTPUTS:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        os.fsync(self.fd)

    def capture(self, path, cancel_check):
        """Copy exact finite bytes under the lease, never transcribe a moving source."""
        self._validate_layout()
        size, digest = 0, hashlib.sha256()
        source = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_AUDIO_BYTES:
                raise CPUASRUnavailable('CPU ASR input is missing, empty or exceeds 64 MiB.')
            with os.fdopen(self._open('input.audio', os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as target:
                for block in iter(lambda: os.read(source, 1024**2), b''):
                    if cancel_check():
                        raise CPUASRUnavailable('CPU ASR cancelled.')
                    size += len(block)
                    if size > MAX_AUDIO_BYTES:
                        raise CPUASRUnavailable('CPU ASR input grew beyond 64 MiB.')
                    digest.update(block)
                    target.write(block)
        finally:
            os.close(source)
        if not size:
            raise CPUASRUnavailable('CPU ASR input became empty.')
        self._validate_layout()
        return self.path / 'input.audio', {'sha256': digest.hexdigest(), 'size': size}

    def run(self, command, cancel_check, timeout=180):
        self._validate_layout()
        run_timing_command(command, cancel_check, timeout, task_lease_fd=self.lease_fd)
        self._validate_layout()

    def ensure_output_absent(self, name):
        """Start a fresh instrument result without accepting a previous pass's file."""
        if name not in ('audio.wav', 'asr.json'):
            raise ValueError('Unknown CPU ASR output.')
        self._validate_layout()
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def _close(self):
        for key in ('lease_fd', 'fd', 'parent_fd'):
            descriptor = getattr(self, key)
            if descriptor is not None:
                setattr(self, key, None)
                os.close(descriptor)

    def __exit__(self, kind, error, traceback):
        try:
            if kind is None and self.admitted:
                self._cleanup()
        finally:
            self.admitted = False
            self._close()

from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
