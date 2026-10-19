"""Cancellable CPU encoding with owned descendants and atomic file publication."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import wave

from subprocess_ownership import start_owned_subprocess


def is_easy_wav_export(format_name, options):
    return (format_name == 'wav' and options.get('codec') is None
            and options.get('parameters') is None)


def write_pcm_wav(raw_path, output_path, settings):
    """Write ordinary easy-WAV PCM in blocks, or defer to the native exporter."""
    if type(settings) is not dict:
        return False
    options = settings.get('options')
    if (type(options) is not dict or type(settings.get('format')) is not str
            or not options.keys() <= {'codec', 'parameters', 'bitrate', 'tags', 'id3v2_version', 'cover'}
            or not is_easy_wav_export(settings['format'], options)):
        return False
    width, rate, channels = (settings.get(key) for key in ('sample_width', 'frame_rate', 'channels'))
    if (any(type(value) is not int or value <= 0 for value in (width, rate, channels))
            or width not in (1, 2, 4)):
        return False
    frame_width = width * channels
    block_size = (65536 // frame_width) * frame_width
    if not block_size:
        return False

    from pydub.audio_segment import audioop
    handles = []
    primary_error = False
    try:
        source = open(raw_path, 'rb')
        handles.append(source)
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size % frame_width:
            return False
        target = open(output_path, 'wb+')
        handles.append(target)
        target.seek(0)
        stream = wave.open(target, 'wb')
        handles.append(stream)
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.setnframes(int(float(info.st_size // frame_width)))
        # Let wave pack the same native header/RIFF fields before reading PCM.
        stream.writeframesraw(b'')
        remaining = info.st_size
        while remaining:
            data = source.read(min(remaining, block_size))
            if not data or len(data) % frame_width:
                raise OSError('PCM snapshot ended before its declared frame count')
            remaining -= len(data)
            if width == 1:
                data = audioop.bias(data, 1, 128)
            stream.writeframesraw(data)
        if source.read(1):
            raise OSError('PCM snapshot grew during WAV export')
        stream.close()
        target.seek(0)
        return True
    except BaseException:
        primary_error = True
        raise
    finally:
        close_error = None
        for handle in reversed(handles):
            try:
                handle.close()
            except BaseException as error:
                if not primary_error and close_error is None:
                    close_error = error
        if close_error is not None:
            raise close_error


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


def export_audio_segment(segment, path, format_name, cancel_check, mastering=None, **kwargs):
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
        if mastering is not None:
            from audio_mastering import export_mastered_pcm
            report = export_mastered_pcm(raw, output, settings, mastering, cancel_check)
            ensure_audio_export_active(cancel_check)
            os.replace(output, destination)
            return report
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
    if write_pcm_wav(raw_path, output_path, settings):
        return
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


def export_pcm_mp3(raw_path, output_path, settings, cancel_check, mastering=None, bitrate='192k'):
    """Encode already-spooled PCM without decoding it into another AudioSegment."""
    from audio_ffmpeg import run_audio_ffmpeg
    from pydub import AudioSegment
    from tts import ensure_audio_export_active
    width, rate, channels = (settings[key] for key in ('sample_width', 'frame_rate', 'channels'))
    if (width not in (1, 2, 4) or any(type(v) is not int or v <= 0 for v in (width, rate, channels))
            or Path(raw_path).stat().st_size != settings['frames'] * width * channels):
        raise ValueError('Invalid staged PCM layout or size.')
    destination = Path(output_path)
    with tempfile.TemporaryDirectory(prefix='.pcm-encode-', dir=destination.parent) as directory:
        output = Path(directory) / 'output.mp3'
        encoder_temp = Path(directory) / 'encoder-temp'; encoder_temp.mkdir()
        environment = dict(os.environ)
        for key in ('TMPDIR', 'TEMP', 'TMP'):
            environment[key] = str(encoder_temp.resolve())
        if mastering is not None:
            from audio_mastering import export_mastered_pcm
            report = export_mastered_pcm(raw_path, output,
                {**settings, 'format': 'mp3', 'options': {'bitrate': bitrate}}, mastering, cancel_check)
        else:
            formats = {1: 's8', 2: 's16le', 4: 's32le'}
            # Preserve the legacy MP3 encoder's cancellation-driven lifetime.
            run_audio_ffmpeg(['-f', formats[width], '-ar', str(rate), '-ac', str(channels),
                              '-i', str(raw_path), '-b:a', bitrate, '-id3v2_version', '3',
                              '-f', 'mp3', str(output)], cancel_check,
                             timeout=None, environment=environment, executable=AudioSegment.converter)
            report = None
        ensure_audio_export_active(cancel_check)
        if not output.is_file() or not output.stat().st_size:
            raise OSError('Encoder did not produce an MP3 file.')
        os.replace(output, destination)
        return report
