"""Cancellable CPU encoding with owned descendants and atomic file publication."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from subprocess_ownership import start_owned_subprocess


def get_encoder_exception(path, fallback):
    """Restore ordinary Pydub/builtin errors for the formerly direct exporter."""
    import builtins
    import struct
    from pydub import exceptions
    try:
        report = json.loads(Path(path).read_text(encoding='utf-8'))
        module = {'builtins': builtins, 'pydub.exceptions': exceptions,
                  'struct': struct}.get(report['module'])
        error_type = getattr(module, report['type'], None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            return error_type(*report['args'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return fallback


def export_audio_segment(segment, path, format_name, cancel_check, **kwargs):
    from tts import ensure_audio_export_active
    from core import ensure_failed_subprocess_stopped
    ensure_audio_export_active(cancel_check)
    destination = Path(path)
    with tempfile.TemporaryDirectory(prefix='.chapter-export-', dir=destination.parent) as directory:
        raw = Path(directory) / 'input.pcm'
        output = Path(directory) / ('output.' + format_name)
        encoder_temp = Path(directory) / 'encoder-temp'
        encoder_temp.mkdir()
        # Only this reaper/encoder tree inherits the private temp location.
        environment = dict(os.environ)
        for name in ('TMPDIR', 'TEMP', 'TMP'):
            environment[name] = str(encoder_temp.resolve())
        data = memoryview(segment.raw_data)
        with raw.open('wb') as stream:
            for start in range(0, len(data), 8 * 1024 * 1024):
                ensure_audio_export_active(cancel_check)
                stream.write(data[start:start + 8 * 1024 * 1024])
        settings = {'sample_width': segment.sample_width, 'frame_rate': segment.frame_rate,
                    'channels': segment.channels, 'format': format_name, 'options': kwargs,
                    'encoder_temp': str(encoder_temp.resolve())}
        if cancel_check is None:
            settings['exception_path'] = str(Path(directory) / 'encoder-error.json')
        process = None
        with tempfile.TemporaryFile() as errors:
            try:
                ensure_audio_export_active(cancel_check)
                process = start_owned_subprocess(
                    [sys.executable, str(Path(__file__).resolve()), str(raw), str(output),
                     json.dumps(settings)], stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True, env=environment)
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
                    error = RuntimeError('Audio encoding failed: ' + errors.read().decode('utf-8', errors='replace'))
                    if cancel_check is None:
                        error = get_encoder_exception(settings['exception_path'], error)
                    raise error
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
    settings = json.loads(sys.argv[3])
    try:
        # Pin the fresh encoder's cache: environment hints alone can fall back
        # outside staging when tempfile's directory probe fails. Allocation
        # errors here must propagate rather than select an unowned directory.
        tempfile.tempdir = settings['encoder_temp']
        run_encoder(sys.argv[1], sys.argv[2], settings)
    except Exception as error:
        if settings.get('exception_path'):
            try:
                Path(settings['exception_path']).write_text(json.dumps({
                    'module': type(error).__module__, 'type': type(error).__name__,
                    'args': error.args}, default=str), encoding='utf-8')
            except (OSError, ValueError, TypeError):
                pass  # Keep the encoder error if its report cannot be written.
        raise
