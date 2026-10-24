"""Bounded seekable audio decoding, independent of model and publication policy."""
import json
import math
import os
from pathlib import Path
import stat

from json_metadata import get_unique_json_object
from audio_source_io import read_bounded_audio_hash
from media_process import run_owned_media_check


def decode_audio_pcm(stream, path, *, frames, sample_rate, channels, source_limit,
                     check_active, consume_pcm=None, cancellation_errors=(), timeout=300, exact_frames=True):
    """Visit finite float64 PCM while retaining the caller's admitted source.

    The caller confines and admits the file and owns its publication decision.
    Sink output is private until this call and the caller's own fences succeed.
    No resampling, downmixing, clipping or original-byte rewriting occurs here.
    With exact_frames=False, frames is an upper bound; the returned count is actual.
    """
    import numpy as np
    if (type(exact_frames) is not bool or type(sample_rate) is not int or not 0 < sample_rate <= 384000
            or type(channels) is not int or channels not in (1, 2)
            or type(frames) is not int or not 0 < frames <= sample_rate * 3600
            or type(source_limit) is not int or not 0 < source_limit <= 1024**3
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 3600):
        raise ValueError('Invalid bounded audio decoding geometry or limits')
    path = Path(os.path.abspath(path))
    before = os.fstat(stream.fileno())
    def version(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    def verify_source():
        check_active()
        try:
            valid = (os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))
                and stat.S_ISREG(before.st_mode) and 0 < before.st_size <= source_limit
                and version(os.fstat(stream.fileno())) == version(before)
                and version(path.lstat()) == version(before))
        except (OSError, ValueError) as error:
            raise ValueError('Audio source changed during decoding') from error
        if not valid:
            raise ValueError('Audio source changed or is not an admitted regular file')
    def fingerprint():
        verify_source()
        position = stream.tell()
        try:
            result = read_bounded_audio_hash(stream, source_limit, check_active)
        finally:
            stream.seek(position)
        verify_source()
        if result[1] != before.st_size:
            raise ValueError('Audio source changed while reading its content')
        return result
    admitted_content = fingerprint()
    def run(command, duration, **options):
        position = stream.tell()
        # Discard buffered read-ahead before handing the kernel descriptor to
        # a child. The child's seek operations share its underlying offset.
        stream.seek(0, os.SEEK_END)
        stream.seek(0)
        try:
            return run_owned_media_check(command, check_active, duration,
                cancellation_errors=cancellation_errors, input_source=stream, **options)
        except OSError as error:
            raise ValueError('Audio decoding requires working ffprobe and ffmpeg executables') from error
        except ValueError as error:
            if 'Protocol not found' in str(error):
                raise ValueError('Update FFmpeg: this build lacks safe seekable descriptor input') from error
            raise
        finally:
            stream.seek(0, os.SEEK_END)
            stream.seek(position)
    raw = run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=sample_rate,channels', '-of', 'json', 'fd:'],
        min(timeout, 60), output_limit=65536)
    document = json.loads(raw, object_pairs_hook=get_unique_json_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite audio metadata')))
    streams = document.get('streams') if isinstance(document, dict) else None
    if (not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict)
            or type(streams[0].get('channels')) is not int
            or streams[0]['channels'] != channels
            or streams[0].get('sample_rate') != str(sample_rate)):
        raise ValueError('Independent audio geometry disagrees with the admitted source')
    verify_source()
    pending = bytearray()
    decoded_frames = 0
    def consume(block):
        nonlocal decoded_frames
        check_active()
        pending.extend(block)
        count = len(pending) // (channels * 8) * (channels * 8)
        if not count:
            return
        samples = np.frombuffer(bytes(pending[:count]), dtype='<f8').reshape(-1, channels)
        del pending[:count]
        decoded_frames += len(samples)
        if decoded_frames > frames or not np.isfinite(samples).all():
            raise ValueError('Decoded audio exceeds its frame allowance or contains non-finite samples')
        if consume_pcm is not None:
            consume_pcm(samples)
    run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect',
        'explode', '-i', 'fd:', '-map', '0:a:0', '-vn', '-sn', '-dn', '-threads', '1',
        '-c:a', 'pcm_f64le', '-f', 'f64le', 'pipe:1'], timeout, output_limit=frames * channels * 8, consume_output=consume)
    if pending or not decoded_frames or (exact_frames and decoded_frames != frames):
        raise ValueError('Decoded audio has incomplete frames or a different frame count')
    if fingerprint() != admitted_content:
        raise ValueError('Audio source changed during independent decoding')
    return dict(frames=decoded_frames, sample_rate=sample_rate, channels=channels)
