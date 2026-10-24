"""Verify a published mix by digest and stream its pinned descriptor in blocks."""
import os
from pathlib import Path
from threading import RLock
from audio_take_history import _open_regular, _version
from pcm_worker_contract import MAX_PCM_BYTES
from project_scope_files import capture_scope_file
from sound_design_cues import _require_digest, _require_choice


class SoundDeliveryStream:
    def __init__(self, stream, version):
        self._stream, self._version = stream, version
        self._remaining, self._lock = version[2], RLock()

    def read(self, size):
        if type(size) is not int or not 1 <= size <= 65536:
            raise ValueError('Sound delivery reads require bounded blocks')
        with self._lock:
            try:
                if self._stream.closed:
                    return b''
                if list(_version(os.fstat(self._stream.fileno()))) != self._version:
                    raise ValueError('Sound delivery changed during download')
                block = self._stream.read(min(size,self._remaining))
                if ((not block and self._remaining)
                        or list(_version(os.fstat(self._stream.fileno()))) != self._version):
                    raise ValueError('Sound delivery changed during download')
                self._remaining -= len(block)
                return block
            except BaseException:
                self._stream.close()
                raise

    def close(self):
        with self._lock:
            self._stream.close()


def open_sound_delivery(root, format_name, sha256):
    """Opening/hash work belongs in a synchronous route or worker thread."""
    _require_choice(format_name,('mp3','wav','m4b'),'Sound delivery format')
    _require_digest(sha256,'Expected sound delivery')
    root = Path(root).absolute()
    filename = 'mixed_audiobook.'+format_name
    facts, _ = capture_scope_file(root,filename,limit=MAX_PCM_BYTES)
    if not facts['exists'] or facts['sha256'] != sha256:
        raise ValueError('Sound delivery is missing or changed; export again')
    stream, before = _open_regular(root/filename,root,MAX_PCM_BYTES)
    try:
        if list(_version(before)) != facts['file_version']:
            raise ValueError('Sound delivery changed before opening')
        return SoundDeliveryStream(stream,facts['file_version']),facts['file_version'][2],'.'+format_name
    except BaseException:
        stream.close()
        raise
