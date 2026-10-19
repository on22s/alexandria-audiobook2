"""Bounded prefix preview; the original is retained for later import."""
import io
import subprocess
import tempfile
import threading
import time
import wave

from audio_channels import get_validated_audio_channel_filter, get_audio_channel_count
from subprocess_ownership import run_owned_capture, start_owned_subprocess, stop_owned_subprocess

PREVIEW_SECONDS = 10
PREVIEW_RATE = 24000
_PREVIEW_SLOTS = threading.BoundedSemaphore(2)


def run_preview_decode(command, cancel_check=None):
    process = None
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = start_owned_subprocess(command, stdout=output, stderr=errors, start_new_session=True)
            while True:
                if cancel_check and cancel_check():
                    raise ValueError('Preview cancelled.')
                if time.monotonic() - started > 60:
                    raise ValueError('Audio preview timed out.')
                try:
                    status = process.wait(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if status:
                raise ValueError('Could not decode the selected audio channel.')
            output.seek(0)
            pcm = output.read(PREVIEW_SECONDS * PREVIEW_RATE * 2 + 1)
            if not pcm or len(pcm) > PREVIEW_SECONDS * PREVIEW_RATE * 2 or len(pcm) % 2:
                raise ValueError('Invalid audio preview length.')
            return pcm
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        stop_owned_subprocess(process, timeout=1)
                finally:
                    control = getattr(process, '_alexandria_control', None)
                    if control is not None:
                        control.close()


def render_audio_channel_preview(path, channel, cancel_check=None):
    if not _PREVIEW_SLOTS.acquire(blocking=False):
        raise ValueError('Two audio previews are already running. Try again when one finishes.')
    try:
        if cancel_check and cancel_check():
            raise ValueError('Preview cancelled.')
        flags = get_validated_audio_channel_filter(path, channel, run_owned_capture)
        channels = get_audio_channel_count(path, run_owned_capture)
        pcm = run_preview_decode(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                                   '-map', '0:a:0', '-vn', '-t', str(PREVIEW_SECONDS),
                                   *flags, '-ac', '1', '-ar', str(PREVIEW_RATE),
                                   '-f', 's16le', 'pipe:1'], cancel_check)
        output = io.BytesIO()
        with wave.open(output, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(PREVIEW_RATE)
            stream.writeframes(pcm)
        return output.getvalue(), channels
    finally:
        _PREVIEW_SLOTS.release()
