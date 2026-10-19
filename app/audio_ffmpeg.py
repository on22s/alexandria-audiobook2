"""Owned, cancellable FFmpeg execution with bounded diagnostic reads."""
import subprocess
import tempfile
import time

from subprocess_ownership import start_owned_subprocess
from tts import ensure_audio_export_active


def run_audio_ffmpeg(arguments, cancel_check=None, timeout=600, environment=None, executable="ffmpeg"):
    from core import ensure_failed_subprocess_stopped
    command = [executable, '-nostdin', '-hide_banner', '-nostats', '-xerror',
               '-threads', '1', '-filter_threads', '1', '-y'] + list(arguments)
    process = None
    started = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        try:
            ensure_audio_export_active(cancel_check)
            process = start_owned_subprocess(command, stdout=subprocess.DEVNULL,
                                             stderr=errors, start_new_session=True, env=environment)
            while True:
                ensure_audio_export_active(cancel_check)
                remaining = timeout - (time.monotonic() - started) if timeout is not None else .1
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    result = process.wait(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            ensure_audio_export_active(cancel_check)
            errors.seek(0, 2); size = errors.tell(); errors.seek(max(0, size - 65536))
            stderr = errors.read(65536).decode('utf-8', errors='replace')
            if result:
                raise RuntimeError('Audio processing failed: ' + stderr[-2048:])
            return stderr
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        ensure_failed_subprocess_stopped(process, {})
                finally:
                    control = getattr(process, '_alexandria_control', None)
                    if control is not None:
                        control.close()
