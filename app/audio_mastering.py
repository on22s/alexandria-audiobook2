"""Optional, measured export processing. All FFmpeg work is owned and cancellable."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time

from audio_mastering_filter import MasteringOptions, get_mastering_options, get_mastering_filter
from tts import ensure_audio_export_active






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


def run_mastering_ffmpeg(arguments, cancel_check=None, timeout=600, *, executable=None):
    from audio_ffmpeg import run_audio_ffmpeg
    if timeout is None:
        raise ValueError('Mastering requires a finite timeout.')
    return run_audio_ffmpeg(arguments, cancel_check, timeout=timeout,
                            **({'executable':executable} if executable is not None else {}))




def get_loudness_measurements(input_args, options, cancel_check=None, *, output_channels=None, executable=None, timeout=600):
    return get_loudnorm_record(run_mastering_ffmpeg(
        [*input_args, '-map', '0:a:0', '-vn', '-af', get_mastering_filter(options, output_channels=output_channels),
         '-f', 'null', '-'], cancel_check, timeout=timeout,
        **({'executable':executable} if executable is not None else {})))


def get_mastering_report(path, options, before, encoded_stderr, cancel_check=None, *, executable=None, timeout=600):
    after = get_loudness_measurements(['-i', str(path)], options, cancel_check, timeout=timeout,
        **({'executable':executable} if executable is not None else {}))
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
    fmt = settings['format']
    if fmt not in ('wav', 'mp3'):
        raise ValueError('Mastering supports MP3 and WAV PCM export.')
    codec = 'libmp3lame' if fmt == 'mp3' else {1: 'pcm_u8', 2: 'pcm_s16le', 4: 'pcm_s32le'}[width]
    from audio_encoding_layout import get_audio_encoding_layout
    output_layout = get_audio_encoding_layout(fmt, rate, channels)
    mix_channels = output_layout['channels'] if output_layout['channels'] != channels else None
    before = get_loudness_measurements(input_args, options, cancel_check, output_channels=mix_channels)
    args = [*input_args, '-map', '0:a:0', '-af', get_mastering_filter(options, before, output_channels=mix_channels),
            '-ar', str(output_layout['frame_rate']), '-ac', str(output_layout['channels']), '-c:a', codec]
    if fmt == 'mp3':
        args += ['-b:a', settings['options'].get('bitrate', '192k')]
    else:
        args += ['-rf64', 'auto']
    stderr = run_mastering_ffmpeg([*args, '-f', fmt, str(output_path)], cancel_check)
    return get_mastering_report(output_path, options, before, stderr, cancel_check)
