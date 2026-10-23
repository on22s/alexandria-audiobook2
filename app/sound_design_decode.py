"""Bounded full-source conversion for privately owned sound asset imports."""
import hashlib
import json
import math
import os
from pathlib import Path

from audio_take_history import _ensure_plain_path, _open_regular
from audio_validation import validate_wav_container
from export_quality import _run_owned_check
from json_metadata import get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_cues import MAX_RATE, MAX_SECONDS
from tts import ensure_audio_export_active

CONVERSION = 'ffmpeg-f64le-normalized-s32le-v1'


def decode_sound_design_asset(root, source_name, output_name, *, source_limit,
                             pcm_limit, cancel_check=None, timeout=300):
    """Write a private canonical file; the caller retains its quota/lifetime lease.

    Failure preserves the partial output for the caller's recovery policy. This
    function neither publishes an asset nor changes a cue or selected book.
    """
    import numpy as np
    if (type(source_limit) is not int or not 0 < source_limit <= 1024**3
            or type(pcm_limit) is not int or not 0 < pcm_limit <= 4 * 1024**3
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 3600):
        raise ValueError('Invalid sound asset resource limits')
    check = lambda: ensure_audio_export_active(cancel_check)
    check()
    before, _ = capture_scope_file(root, source_name, limit=source_limit,
                                   cancel_check=cancel_check)
    if not before['exists'] or not before['size']:
        raise ValueError('Sound asset source is missing or empty')
    root = Path(os.path.abspath(root))
    source = root / source_name
    # Use the same confined-name validation for a not-yet-created destination.
    target, _ = capture_scope_file(root, output_name, limit=pcm_limit,
                                   cancel_check=cancel_check)
    if target['exists']:
        raise ValueError('Sound asset output already exists')
    output = _ensure_plain_path(root / output_name, root)
    with _open_regular(source, root, source_limit)[0] as stream:
        validate_wav_container(stream, 'Sound asset', cancel_check=cancel_check)
    raw = _run_owned_check([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
        'stream=sample_rate,channels,codec_name:format=format_name',
        '-of', 'json', str(source)], cancel_check, min(timeout, 60), output_limit=65536)
    document = json.loads(raw, object_pairs_hook=get_unique_json_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite media metadata')))
    if not isinstance(document, dict):
        raise ValueError('Invalid sound asset media metadata')
    streams = document.get('streams')
    container = document.get('format')
    if (not isinstance(streams, list) or len(streams) != 1
            or not isinstance(streams[0], dict) or not isinstance(container, dict)):
        raise ValueError('Sound asset needs one selected audio stream')
    row = streams[0]
    rate, channels = row.get('sample_rate'), row.get('channels')
    format_name, codec = container.get('format_name'), row.get('codec_name')
    if (not isinstance(rate, str) or not rate.isascii() or not rate.isdigit()
            or len(rate) > 6 or not 0 < int(rate) <= MAX_RATE
            or type(channels) is not int or channels not in (1, 2)
            or format_name not in ('wav', 'flac', 'mp3', 'ogg')
            or not isinstance(codec, str) or not 0 < len(codec) <= 64):
        raise ValueError('Unsupported sound asset format or geometry')
    rate = int(rate)
    maximum_frames = min(pcm_limit // (channels * 4), rate * MAX_SECONDS)
    if not maximum_frames:
        raise ValueError('Sound asset allowance cannot hold one frame')
    digest = hashlib.sha256()
    frames = 0
    pending = bytearray()
    with output.open('xb') as destination:
        def consume(block):
            nonlocal frames
            check()
            pending.extend(block)
            count = len(pending) // (channels * 8) * (channels * 8)
            if not count:
                return
            values = np.frombuffer(bytes(pending[:count]), dtype='<f8')
            del pending[:count]
            if not np.isfinite(values).all() or (values < -1).any() or (values > 1).any():
                raise ValueError('Sound asset contains invalid or out-of-range samples')
            frames += len(values) // channels
            if frames > maximum_frames:
                raise ValueError('Sound asset exceeds its decoded allowance')
            canonical = np.clip(np.rint(values * 2147483648),
                                -2147483648, 2147483647).astype('<i4').tobytes()
            if destination.write(canonical) != len(canonical):
                raise OSError('Incomplete sound asset PCM write')
            digest.update(canonical)
        _run_owned_check([
            'ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
            '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
            '-threads', '1', '-c:a', 'pcm_f64le', '-f', 'f64le', 'pipe:1'],
            cancel_check, timeout, output_limit=maximum_frames * channels * 8,
            consume_output=consume)
        if pending or not frames:
            raise ValueError('Sound asset decoder returned incomplete PCM frames')
        check()
        destination.flush()
        os.fsync(destination.fileno())
    after, _ = capture_scope_file(root, source_name, limit=source_limit,
                                  cancel_check=cancel_check)
    if after != before:
        raise ValueError('Sound asset source changed during import')
    check()
    return dict(conversion=CONVERSION, source=before, sha256=digest.hexdigest(),
                frames=frames, frame_rate=rate, channels=channels, sample_width=4,
                bytes=frames * channels * 4, codec=codec, format=format_name)
