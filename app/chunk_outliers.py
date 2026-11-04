"""CPU proxies, not a real-speech quality qualification.

RMS dBFS uses active frames, nuclei rate uses elapsed seconds, voiced ratio
uses active frames only. Fixed conservative spread floors: 3 dB, 1 nucleus/s,
25 Hz, 0.15 ratio. Unknown metric z and flagged are None; global False means
no measured outlier, NOT clean. PCM is mono normalized float or signed int16.
"""
import math
from pathlib import Path
import shutil
import subprocess

import numpy as np

from media_process import run_owned_media_check

OUTLIER_SIGMA = 2.0
MIN_GROUP_SIZE = 8
MAX_DURATION_SECONDS = 60
MAX_SAMPLE_RATE = 48000
MAX_SAMPLES = MAX_DURATION_SECONDS * MAX_SAMPLE_RATE
DECODE_SAMPLE_RATE = 16000
DECODE_TIMEOUT_SECONDS = 20
OUTLIER_REGISTRY = {
    'loudness_db': ('RMS loudness proxy (dB, not LUFS)', 3.0),
    'syllable_rate': ('Approximate syllable rate (energy-envelope proxy)', 1.0),
    'median_pitch_hz': ('Approximate median voiced pitch (Hz)', 25.0),
    'voiced_ratio': ('Approximate voiced-frame ratio', 0.15),
}
OUTLIER_METRICS = tuple(OUTLIER_REGISTRY)
OUTLIER_LABELS = {metric: spec[0] for metric, spec in OUTLIER_REGISTRY.items()}
SPREAD_FLOORS = {metric: spec[1] for metric, spec in OUTLIER_REGISTRY.items()}


def _unavailable(error):
    return {**dict.fromkeys(SPREAD_FLOORS), 'error': str(error)}


def measure_take_acoustics(pcm, sample_rate):
    """Measure bounded CPU proxies; invalid input never yields zero defaults."""
    try:
        if (isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer))
                or not 8000 <= sample_rate <= MAX_SAMPLE_RATE):
            raise ValueError('Sample rate must be an integer from 8000 to 48000 Hz.')
        if not isinstance(pcm, np.ndarray) or pcm.ndim != 1 or not 1 <= pcm.size <= MAX_SAMPLES:
            raise ValueError('PCM must be a nonempty bounded mono numpy array.')
        if pcm.size > sample_rate * MAX_DURATION_SECONDS:
            raise ValueError('PCM exceeds duration limit.')
        if pcm.dtype == np.int16:
            signal = pcm.astype(np.float64) / 32768.0
        elif np.issubdtype(pcm.dtype, np.floating):
            signal = pcm.astype(np.float64)
        else:
            raise ValueError('PCM must be normalized float or signed int16.')
        if not np.all(np.isfinite(signal)) or np.max(np.abs(signal)) > 1:
            raise ValueError('PCM must contain finite normalized samples.')
        signal -= np.mean(signal)
        block_size = sample_rate // 100
        block_count = signal.size // block_size
        if block_count < 10:
            raise ValueError('At least 100 ms of PCM is required.')
        envelope = np.sqrt(np.mean(signal[:block_count * block_size].reshape(
            block_count, block_size) ** 2, axis=1))
        threshold = max(1e-5, float(envelope.max()) * 0.08)
        active = envelope > threshold
        if not np.any(active):
            return _unavailable('No signal: silence or constant DC.')
        loudness = 20 * math.log10(float(np.sqrt(np.mean(envelope[active] ** 2))))
        smooth = np.convolve(np.pad(envelope, (2, 2), mode='edge'), np.ones(5) / 5, mode='valid')
        peaks = np.flatnonzero((smooth[1:-1] >= smooth[:-2]) & (smooth[1:-1] > smooth[2:]) &
                               (smooth[1:-1] > smooth.max() * 0.3)) + 1
        nuclei = []
        for peak in sorted(peaks, key=lambda index: -smooth[index]):
            if all(abs(int(peak) - other) * block_size / sample_rate >= 0.12 for other in nuclei):
                nuclei.append(int(peak))
        reduced = np.interp(np.arange(0, signal.size, sample_rate / 4000),
                            np.arange(signal.size), signal)
        pitches, active_frames = [], 0
        frame_size = 256
        for start in range(0, reduced.size - frame_size + 1, 80):
            frame = reduced[start:start + frame_size].copy()
            frame -= frame.mean()
            if np.sqrt(np.mean(frame ** 2)) <= threshold:
                continue
            active_frames += 1
            correlation = np.correlate(frame, frame, mode='full')[frame_size - 1:]
            lags = np.arange(8, 81)
            energy = np.cumsum(np.r_[0.0, frame ** 2])
            denominator = np.sqrt(energy[frame_size - lags] * (energy[frame_size] - energy[lags]))
            values = correlation[lags] / np.maximum(denominator, 1e-15)
            candidates = np.flatnonzero((values[1:-1] >= values[:-2]) &
                                        (values[1:-1] > values[2:])) + 1
            candidates = candidates[values[candidates] >= max(0.65, values.max() * 0.9)]
            if candidates.size:
                pitches.append(4000 / float(lags[candidates[0]]))
        if not active_frames:
            return {**_unavailable('Insufficient active frames.'), 'loudness_db': loudness}
        return dict(loudness_db=loudness, syllable_rate=len(nuclei) / (signal.size / sample_rate),
                    median_pitch_hz=float(np.median(pitches)) if pitches else None,
                    voiced_ratio=len(pitches) / active_frames,
                    error=None if pitches else 'No reliable periodic pitch (unvoiced signal).')
    except (ValueError, TypeError, OverflowError) as error:
        return _unavailable(error)


def get_outlier_scores(measurements, sigma=OUTLIER_SIGMA):
    """Pure aligned robust scores; unavailable metrics remain explicitly unknown."""
    if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError('sigma must be finite and positive.')
    def finite(value):
        return (isinstance(value, (int, float, np.integer, np.floating))
                and not isinstance(value, (bool, np.bool_)) and math.isfinite(value))
    groups = {}
    for metric, floor in SPREAD_FLOORS.items():
        values = [item.get(metric) for item in measurements if finite(item.get(metric))]
        if len(values) >= MIN_GROUP_SIZE:
            median = float(np.median(values))
            spread = max(floor, 1.4826 * float(np.median(np.abs(np.asarray(values) - median))))
            groups[metric] = median, spread
    results = []
    for item in measurements:
        metrics, errors = {}, [str(item['error'])] if item.get('error') else []
        for metric in SPREAD_FLOORS:
            value = float(item[metric]) if finite(item.get(metric)) else None
            median, spread = groups.get(metric, (None, None))
            measured = value is not None and spread is not None
            z = (value - median) / spread if measured else None
            metrics[metric] = dict(value=value, median=median, spread=spread, z=z,
                                   flagged=abs(z) > sigma if measured else None)
            if not measured:
                errors.append(metric + ': unavailable value or fewer than 8 finite group members')
        results.append(dict(metrics=metrics, flagged=any(record['flagged'] is True for record in metrics.values()),
                            error='; '.join(errors) if errors else None))
    return results


def decode_take_acoustics(path, check_active=None):
    """Probe then decode bounded local mono16k PCM; reject, never truncate."""
    try:
        source = Path(path).resolve(strict=True)
        if not source.is_file():
            raise ValueError('Audio path must be a regular file.')
        executable, probe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not executable or not probe:
            raise ValueError('ffmpeg and ffprobe are required.')
        duration_bytes = run_owned_media_check(
            [probe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries',
             'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)],
            check_active or (lambda: None), DECODE_TIMEOUT_SECONDS, output_limit=1024)
        duration = float(duration_bytes.strip())
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
            raise ValueError('Audio duration is unknown, empty or exceeds duration limit.')
        decoded = run_owned_media_check(
            [executable, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
             '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar',
             str(DECODE_SAMPLE_RATE), '-f', 's16le', '-threads', '1', 'pipe:1'],
            check_active or (lambda: None), DECODE_TIMEOUT_SECONDS,
            output_limit=MAX_DURATION_SECONDS * DECODE_SAMPLE_RATE * 2)
        if not decoded or len(decoded) % 2:
            raise ValueError('Decoder returned empty or incomplete PCM.')
        return measure_take_acoustics(np.frombuffer(decoded, dtype='<i2'), DECODE_SAMPLE_RATE)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        return _unavailable('Audio decode failed: ' + str(error)[:512])
