"""Cancellable CPU encoding with owned descendants and atomic file publication."""
import copy
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import wave

from subprocess_ownership import start_owned_subprocess
from media_process import get_media_process_environment


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
        source = _open_encoder_file(raw_path, 'rb', settings)
        handles.append(source)
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size % frame_width:
            return False
        target = _open_encoder_file(output_path, 'wb+', settings)
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
        _record_encoder_output(target, output_path, settings)
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


def _open_encoder_file(path, mode, settings):
    """Open a pre-created registered inode without following or truncating aliases."""
    identities = settings.get('registered_files')
    if identities is None:
        return open(path, mode, **({'encoding': 'utf-8'} if 'b' not in mode else {}))
    format_name = settings.get('format')
    if (format_name not in ('mp3', 'wav') or type(identities) is not dict
            or set(identities) != {'input.pcm', 'output.' + format_name, 'encoder-error.json'}
            or any(type(value) not in (list, tuple) or len(value) != 2
                   or any(type(part) is not int or not 0 <= part < 2**64 for part in value)
                   for value in identities.values())
            or Path(path).name not in identities or mode not in ('rb', 'wb+', 'w')):
        raise ValueError('Invalid registered encoder file authority')
    descriptor = os.open(path, (os.O_RDONLY if mode == 'rb' else os.O_RDWR)
                         | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != tuple(identities[Path(path).name])):
            raise ValueError('Registered encoder file identity changed')
        if mode != 'rb':
            os.ftruncate(descriptor, 0)
        stream = os.fdopen(descriptor, mode, **({'encoding': 'utf-8'} if 'b' not in mode else {}))
        descriptor = None
        return stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _pin_encoder_temp(settings):
    """Pin registered scratch as this child tree's cwd, without inherited FDs."""
    identity = settings.get('encoder_temp_identity')
    if identity is None:
        tempfile.tempdir = settings['encoder_temp']
        return
    directory = settings.get('encoder_temp')
    if (type(identity) not in (list, tuple) or len(identity) != 2
            or any(type(value) is not int or not 0 <= value < 2**64 for value in identity)
            or not isinstance(directory, str) or not os.path.isabs(directory)):
        raise ValueError('Invalid registered encoder directory authority')
    if (len(sys.path) > 256 or any(not isinstance(entry, str) or len(entry) > 32768
                                    for entry in sys.path)):
        raise ValueError('Invalid registered encoder import paths')
    # Freeze imports against the original application cwd before scratch
    # becomes cwd. Imports (including lazy Pydub imports) must never search it.
    sys.path[:] = [os.path.abspath(entry or os.curdir) for entry in sys.path]
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) != tuple(identity) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise ValueError('Registered encoder directory identity changed')
        os.fchdir(descriptor)
    finally:
        os.close(descriptor)
    # Cwd pins the directory for both Pydub and its codec children. Absolute
    # media paths stay unchanged; no /proc path or reaper pass_fds is required.
    tempfile.tempdir = '.'
    settings['encoder_temp'] = '.'
    for name in ('TMPDIR', 'TEMP', 'TMP'):
        os.environ[name] = '.'


def _record_encoder_output(target, output_path, settings):
    if settings.get('registered_files') is None:
        return
    target.flush()
    info = os.fstat(target.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or (info.st_dev, info.st_ino) != tuple(settings['registered_files'][Path(output_path).name])):
        raise ValueError('Registered encoder output changed while writing')
    version = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
               info.st_ctime_ns, info.st_mode, info.st_nlink, info.st_uid]
    with _open_encoder_file(settings['exception_path'], 'w', settings) as report:
        report.write(json.dumps({'status': 'success', 'output': Path(output_path).name, 'version': version}))


def _read_encoder_report(path, settings=None, max_bytes=65536):
    if type(max_bytes) is not int or not 0 < max_bytes <= 65536:
        raise ValueError('Invalid encoder report limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate encoder report field')
            result[key] = value
        return result
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Nonfinite encoder report')
        return number
    if settings is not None:
        if type(settings) is not dict or type(settings.get('registered_files')) is not dict:
            raise ValueError('Missing registered encoder report authority')
        stream = _open_encoder_file(path, 'rb', settings)
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('Encoder report is not a regular file')
            stream = os.fdopen(descriptor, 'rb')
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    with stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('Encoder report exceeds its byte limit')
    return json.loads(raw, object_pairs_hook=unique, parse_float=finite_float,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite encoder report')))


def _get_encoder_output_version(settings, output_name):
    from synthesis_workspace import SynthesisWorkspaceUnavailable
    try:
        report = _read_encoder_report(settings['exception_path'], settings, 4096)
        if (type(report) is not dict or set(report) != {'status', 'output', 'version'}
                or report['status'] != 'success' or report['output'] != output_name):
            raise ValueError('Invalid encoder completion report')
        version = report['version']
        if (type(version) is not list or len(version) != 8
                or any(type(value) is not int or not -(2**63) <= value < 2**64 for value in version)
                or any(version[index] < 0 for index in (0, 1, 2, 5, 6, 7))
                or not stat.S_ISREG(version[5]) or version[6] != 1 or version[7] != os.geteuid()
                or tuple(version[:2]) != tuple(settings['registered_files'][output_name])):
            raise ValueError('Invalid encoder completion version')
        return tuple(version)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise SynthesisWorkspaceUnavailable('Registered encoder completion report is invalid') from error


def get_encoder_exception(path, fallback, settings=None):
    """Restore ordinary Pydub/builtin errors for the formerly direct exporter."""
    import builtins
    import struct
    from pydub import exceptions
    try:
        report = _read_encoder_report(path, settings)
        if (type(report) is not dict or not isinstance(report.get('module'), str)
                or not isinstance(report.get('type'), str) or type(report.get('args')) is not list):
            return fallback
        module = {'builtins': builtins, 'pydub.exceptions': exceptions,
                  'struct': struct}.get(report['module'])
        error_type = getattr(module, report['type'], None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            return error_type(*report['args'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        pass
    return fallback


def ensure_export_quality_output(path, plan, callback, cancel_check):
    """Check a requested assembled delivery and transport its private receipt."""
    if plan is None:
        if callback is not None:
            raise ValueError('Export quality callback requires a PCM expectation plan.')
        return
    from export_quality import validate_finished_export, ensure_export_stage_current
    report = validate_finished_export(path, plan, cancel_check)
    if callback is not None:
        callback(copy.deepcopy(report))
    ensure_export_stage_current(path, report, cancel_check)


def export_audio_segment(segment, path, format_name, cancel_check, mastering=None,
                         quality_plan=None, quality_callback=None, workspace=None, **kwargs):
    from tts import ensure_audio_export_active
    from core import ensure_failed_subprocess_stopped
    ensure_audio_export_active(cancel_check)
    destination = Path(path)
    registered_files = {}
    destination_identity = None
    encoder_cwd = os.getcwd() if workspace is not None else None
    if workspace is not None:
        from synthesis_workspace import RegisteredEncodingStage, SynthesisWorkspaceUnavailable
        if type(workspace) is not RegisteredEncodingStage:
            raise ValueError('Audio encoding requires live registered workspace authority')
        if mastering is not None:
            raise SynthesisWorkspaceUnavailable('Registered chunk encoding does not grant mastering authority')
        directory, owned_temp = workspace.export_layout(destination, format_name)
        info = workspace.check_member(destination.name)
        destination_identity = (info.st_dev, info.st_ino)
        scope = nullcontext(directory)
    else:
        scope = tempfile.TemporaryDirectory(prefix='.chapter-export-', dir=destination.parent)
    with scope as directory:
        raw = Path(directory) / 'input.pcm'
        output = Path(directory) / ('output.' + format_name)
        encoder_temp = owned_temp if workspace is not None else Path(directory) / 'encoder-temp'
        if workspace is None:
            encoder_temp.mkdir()
        # Only this reaper/encoder tree inherits the private temp location.
        environment = get_media_process_environment()
        temporary_path = str(encoder_temp) if workspace is not None else str(encoder_temp.resolve())
        for name in ('TMPDIR', 'TEMP', 'TMP'):
            environment[name] = temporary_path
        data = memoryview(segment.raw_data)
        def create_registered(name):
            try:
                descriptor = workspace.create_file(name)
            except OSError as error:
                raise SynthesisWorkspaceUnavailable('Encoder member already exists or is unsafe') from error
            try:
                info = os.fstat(descriptor)
                registered_files[name] = (info.st_dev, info.st_ino)
                return os.fdopen(descriptor, 'wb')
            except BaseException:
                os.close(descriptor)
                raise

        with (create_registered('input.pcm') if workspace is not None
              else raw.open('wb')) as stream:
            for start in range(0, len(data), 8 * 1024 * 1024):
                ensure_audio_export_active(cancel_check)
                stream.write(data[start:start + 8 * 1024 * 1024])
        if quality_plan is not None and format_name == 'mp3':
            from audio_encoding_layout import get_audio_encoding_layout
            layout = get_audio_encoding_layout('mp3', segment.frame_rate, segment.channels)
            kwargs = {**kwargs, 'parameters': [*(kwargs.get('parameters') or []),
                '-ar', str(layout['frame_rate']), '-ac', str(layout['channels'])]}
        settings = {'sample_width': segment.sample_width, 'frame_rate': segment.frame_rate,
                    'channels': segment.channels, 'format': format_name, 'options': kwargs,
                    'encoder_temp': temporary_path}
        if workspace is not None:
            for name in (output.name, 'encoder-error.json'):
                with create_registered(name):
                    pass
            settings['registered_files'] = registered_files
            settings['encoder_temp_identity'] = workspace.join_identity
        if cancel_check is None or workspace is not None:
            settings['exception_path'] = str(Path(directory) / 'encoder-error.json')
        if mastering is not None:
            from audio_mastering import export_mastered_pcm
            report = export_mastered_pcm(raw, output, settings, mastering, cancel_check)
            ensure_export_quality_output(output, quality_plan, quality_callback, cancel_check)
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
                    start_new_session=True, env=environment,
                    **({'cwd': encoder_cwd} if workspace is not None else {}))
                while True:
                    ensure_audio_export_active(cancel_check)
                    try:
                        result = process.wait(timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if workspace is not None:
                    for name, identity in registered_files.items():
                        workspace.check_member(name, identity)
                    workspace.check_member(destination.name, destination_identity)
                if result:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 4096))
                    error = RuntimeError('Audio encoding failed: ' + errors.read().decode('utf-8', errors='replace'))
                    if cancel_check is None:
                        if workspace is not None:
                            error = get_encoder_exception(settings['exception_path'], error, settings)
                        else:
                            error = get_encoder_exception(settings['exception_path'], error)
                    raise error
                ensure_export_quality_output(output, quality_plan, quality_callback, cancel_check)
                ensure_audio_export_active(cancel_check)
                if workspace is not None:
                    for name, identity in registered_files.items():
                        workspace.check_member(name, identity)
                    workspace.check_member(destination.name, destination_identity)
                if workspace is not None:
                    workspace.finish_encoder_output(output.name, destination.name,
                        _get_encoder_output_version(settings, output.name), destination_identity)
                else:
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
    with _open_encoder_file(raw_path, 'rb', settings) as source:
        data = source.read()
    segment = AudioSegment(data=data,
                           sample_width=settings['sample_width'],
                           frame_rate=settings['frame_rate'], channels=settings['channels'])
    with _open_encoder_file(output_path, 'wb+', settings) as target:
        segment.export(target, format=settings['format'], **settings['options'])
        _record_encoder_output(target, output_path, settings)


if __name__ == '__main__':
    settings = json.loads(sys.argv[3])
    try:
        # Pin the fresh encoder's cache: environment hints alone can fall back
        # outside staging when tempfile's directory probe fails. Allocation
        # errors here must propagate rather than select an unowned directory.
        _pin_encoder_temp(settings)
        run_encoder(sys.argv[1], sys.argv[2], settings)
    except Exception as error:
        if settings.get('exception_path'):
            try:
                with _open_encoder_file(settings['exception_path'], 'w', settings) as target:
                    target.write(json.dumps({'module': type(error).__module__,
                        'type': type(error).__name__, 'args': error.args}, default=str))
            except (OSError, ValueError, TypeError):
                pass  # Keep the encoder error if its report cannot be written.
        raise


def export_pcm_mp3(raw_path, output_path, settings, cancel_check, mastering=None, bitrate='192k',
                   quality_plan=None, quality_callback=None):
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
        environment = get_media_process_environment()
        for key in ('TMPDIR', 'TEMP', 'TMP'):
            environment[key] = str(encoder_temp.resolve())
        if mastering is not None:
            from audio_mastering import export_mastered_pcm
            report = export_mastered_pcm(raw_path, output,
                {**settings, 'format': 'mp3', 'options': {'bitrate': bitrate}}, mastering, cancel_check)
        else:
            formats = {1: 's8', 2: 's16le', 4: 's32le'}
            output_parameters = []
            if quality_plan is not None:
                from audio_encoding_layout import get_audio_encoding_layout
                layout = get_audio_encoding_layout('mp3', rate, channels)
                output_parameters = ['-ar', str(layout['frame_rate']), '-ac', str(layout['channels'])]
            # Preserve the legacy MP3 encoder's cancellation-driven lifetime.
            run_audio_ffmpeg(['-f', formats[width], '-ar', str(rate), '-ac', str(channels),
                              '-i', str(raw_path), '-b:a', bitrate, '-id3v2_version', '3',
                              *output_parameters, '-f', 'mp3', str(output)], cancel_check,
                             timeout=None, environment=environment, executable=AudioSegment.converter)
            report = None
        ensure_audio_export_active(cancel_check)
        if not output.is_file() or not output.stat().st_size:
            raise OSError('Encoder did not produce an MP3 file.')
        ensure_export_quality_output(output, quality_plan, quality_callback, cancel_check)
        ensure_audio_export_active(cancel_check)
        os.replace(output, destination)
        return report


_RAW_EXPORT_AUDIO_SEGMENT = export_audio_segment
