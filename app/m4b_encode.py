"""Owned, cancellable FFmpeg M4B encoding with progress from the encoder."""
import os
from pathlib import Path
import subprocess
import tempfile
import time

from subprocess_ownership import start_owned_subprocess
from tts import ensure_audio_export_active


def encode_m4b(command, duration_seconds, cancel_check=None, progress_callback=None, timeout=600):
    from core import ensure_failed_subprocess_stopped
    ensure_audio_export_active(cancel_check)
    process = None
    with tempfile.TemporaryDirectory(prefix='.m4b-progress-', dir=Path(command[-1]).parent) as directory:
        progress_path = Path(directory) / 'progress.txt'
        progress_path.touch()
        cmd = command[:-1] + ['-nostats', '-progress', str(progress_path), command[-1]]
        started = time.monotonic()
        last_encoded = -1
        with tempfile.TemporaryFile() as errors, progress_path.open(encoding='utf-8') as progress:
            try:
                process = start_owned_subprocess(cmd, stdout=subprocess.DEVNULL, stderr=errors,
                                                 start_new_session=True)
                while True:
                    ensure_audio_export_active(cancel_check)
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    while True:
                        offset = progress.tell()
                        line = progress.readline()
                        if not line.endswith("\n"):
                            progress.seek(offset)
                            break
                        if not line.startswith('out_time_us='):
                            continue
                        try:
                            encoded = int(line.strip().split('=', 1)[1]) / 1_000_000
                        except ValueError:
                            continue
                        if encoded >= 0 and encoded > last_encoded:
                            last_encoded = encoded
                            if progress_callback:
                                progress_callback(f'Encoding M4B: {encoded:.1f}/{duration_seconds:.1f} s; elapsed {elapsed:.1f} s')
                    try:
                        result = process.wait(timeout=min(.1, timeout - elapsed))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                ensure_audio_export_active(cancel_check)
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4096))
                return result, errors.read().decode('utf-8', errors='replace')
            finally:
                if process is not None:
                    try:
                        if process.poll() is None:
                            ensure_failed_subprocess_stopped(process, {})
                    finally:
                        control = getattr(process, '_alexandria_control', None)
                        if control is not None:
                            control.close()
