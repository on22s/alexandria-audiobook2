"""Cancellable CPU encoding with owned descendants and atomic file publication."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from subprocess_ownership import start_owned_subprocess


def export_audio_segment(segment, path, format_name, cancel_check, **kwargs):
    from tts import ensure_audio_export_active
    from core import ensure_failed_subprocess_stopped
    ensure_audio_export_active(cancel_check)
    destination = Path(path)
    with tempfile.TemporaryDirectory(prefix='.chapter-export-', dir=destination.parent) as directory:
        raw = Path(directory) / 'input.pcm'
        output = Path(directory) / ('output.' + format_name)
        data = memoryview(segment.raw_data)
        with raw.open('wb') as stream:
            for start in range(0, len(data), 8 * 1024 * 1024):
                ensure_audio_export_active(cancel_check)
                stream.write(data[start:start + 8 * 1024 * 1024])
        settings = {'sample_width': segment.sample_width, 'frame_rate': segment.frame_rate,
                    'channels': segment.channels, 'format': format_name, 'options': kwargs}
        process = None
        with tempfile.TemporaryFile() as errors:
            try:
                ensure_audio_export_active(cancel_check)
                process = start_owned_subprocess(
                    [sys.executable, str(Path(__file__).resolve()), str(raw), str(output),
                     json.dumps(settings)], stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True)
                while True:
                    ensure_audio_export_active(cancel_check)
                    try:
                        result = process.wait(timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if result:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 4096))
                    raise RuntimeError('Audio encoding failed: ' + errors.read().decode('utf-8', errors='replace'))
                ensure_audio_export_active(cancel_check)
                os.replace(output, destination)
            finally:
                if process is not None:
                    try:
                        if process.poll() is None:
                            ensure_failed_subprocess_stopped(process, {})
                    finally:
                        control = getattr(process, '_alexandria_control', None)
                        if control is not None:
                            control.close()


def run_encoder(raw_path, output_path, settings):
    from pydub import AudioSegment
    segment = AudioSegment(data=Path(raw_path).read_bytes(),
                           sample_width=settings['sample_width'],
                           frame_rate=settings['frame_rate'], channels=settings['channels'])
    with open(output_path, 'wb+') as target:
        segment.export(target, format=settings['format'], **settings['options'])


if __name__ == '__main__':
    run_encoder(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
