"""Optional, measured export processing. All FFmpeg work is owned and cancellable."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from subprocess_ownership import start_owned_subprocess
from tts import ensure_audio_export_active


class MasteringOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_default=True)
    loudness: FiniteFloat = Field(default=-18, ge=-24, le=-14)
    true_peak: FiniteFloat = Field(default=-2, ge=-9, le=-1)


def get_mastering_options(options):
    if options is None:
        return None
    return MasteringOptions.model_validate(options).model_dump()


def get_audio_sha256(path, cancel_check=None):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            ensure_audio_export_active(cancel_check)
            digest.update(block)
    return digest.hexdigest()


def get_loudnorm_record(stderr):
    records = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', stderr)
    if not records:
        raise ValueError('FFmpeg did not return loudness measurements.')
    record = json.loads(records[-1])
    for key in ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset'):
        value = float(record[key])
        if not math.isfinite(value):
            raise ValueError('Audio has no finite loudness measurement (silence or invalid audio).')
    return record


def run_mastering_ffmpeg(arguments, cancel_check=None, timeout=600):
    from core import ensure_failed_subprocess_stopped
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-xerror',
               '-threads', '1', '-filter_threads', '1', '-y'] + list(arguments)
    process = None
    started = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        try:
            ensure_audio_export_active(cancel_check)
            process = start_owned_subprocess(command, stdout=subprocess.DEVNULL,
                                             stderr=errors, start_new_session=True)
            while True:
                ensure_audio_export_active(cancel_check)
                remaining = timeout - (time.monotonic() - started)
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
                raise RuntimeError('Mastering/measurement failed: ' + stderr[-2048:])
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


def get_mastering_filter(options, measured=None):
    options = get_mastering_options(options)
    value = f"loudnorm=I={options['loudness']}:TP={options['true_peak']}:LRA=11"
    if measured is not None:
        # Only numeric values from our strict record parser enter this filter.
        for option, key in [('measured_I', 'input_i'), ('measured_TP', 'input_tp'),
                            ('measured_LRA', 'input_lra'), ('measured_thresh', 'input_thresh'),
                            ('offset', 'target_offset')]:
            value += f':{option}={float(measured[key])}'
        value += ':linear=true'
    return value + ':print_format=json'


def get_loudness_measurements(input_args, options, cancel_check=None):
    return get_loudnorm_record(run_mastering_ffmpeg(
        [*input_args, '-map', '0:a:0', '-vn', '-af', get_mastering_filter(options), '-f', 'null', '-'], cancel_check))


def get_mastering_report(path, options, before, encoded_stderr, cancel_check=None):
    after = get_loudness_measurements(['-i', str(path)], options, cancel_check)
    encoded = get_loudnorm_record(encoded_stderr)
    mode = encoded.get('normalization_type')
    if mode not in ('linear', 'dynamic'):
        raise ValueError('FFmpeg did not identify the normalization mode.')
    def metrics(record):
        return {'integrated_lufs': float(record['input_i']), 'true_peak_dbtp': float(record['input_tp']),
                'range_lu': float(record['input_lra'])}
    return {'settings': get_mastering_options(options), 'before': metrics(before), 'after': metrics(after),
            'mode': mode, 'sha256': get_audio_sha256(path, cancel_check)}


def export_mastered_pcm(raw_path, output_path, settings, options, cancel_check=None):
    width, rate, channels = (settings[key] for key in ('sample_width', 'frame_rate', 'channels'))
    if width not in (1, 2, 4) or not 8000 <= rate <= 192000 or not 1 <= channels <= 32:
        raise ValueError('Unsupported mastering PCM format.')
    if Path(raw_path).stat().st_size % (width * channels):
        raise ValueError('Mastering PCM snapshot has an incomplete frame.')
    formats = {1: 's8', 2: 's16le', 4: 's32le'}
    input_args = ['-f', formats[width], '-ar', str(rate), '-ac', str(channels), '-i', str(raw_path)]
    before = get_loudness_measurements(input_args, options, cancel_check)
    fmt = settings['format']
    if fmt not in ('wav', 'mp3'):
        raise ValueError('Mastering supports MP3 and WAV PCM export.')
    codec = 'libmp3lame' if fmt == 'mp3' else {1: 'pcm_u8', 2: 'pcm_s16le', 4: 'pcm_s32le'}[width]
    args = [*input_args, '-map', '0:a:0', '-af', get_mastering_filter(options, before),
            '-ar', str(rate), '-ac', str(channels), '-c:a', codec]
    if fmt == 'mp3':
        args += ['-b:a', settings['options'].get('bitrate', '192k')]
    else:
        args += ['-rf64', 'auto']
    stderr = run_mastering_ffmpeg([*args, '-f', fmt, str(output_path)], cancel_check)
    return get_mastering_report(output_path, options, before, stderr, cancel_check)
