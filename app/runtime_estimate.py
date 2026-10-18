"""Project the finished audiobook's runtime and size from rendered chunks (issue #1330)."""
import math

from project import MP3_BITRATE
from tts import get_pause_duration_ms

# The TTS pipeline writes 16-bit mono WAV at 24 kHz.
WAV_SAMPLE_RATE = 24000
WAV_BYTES_PER_SAMPLE = 2
WAV_BYTES_PER_SECOND = WAV_SAMPLE_RATE * WAV_BYTES_PER_SAMPLE
FALLBACK_CHARS_PER_SECOND = 14.0


def get_bitrate_bytes_per_second(bitrate):
    """Parse a project bitrate such as '128k' into bytes per second."""
    text = str(bitrate).strip().lower()
    if text.endswith('k'):
        text = text[:-1]
        return float(text) * 1000.0 / 8.0
    return float(text) / 8.0


MP3_BYTES_PER_SECOND = get_bitrate_bytes_per_second(MP3_BITRATE)


def get_percentile(values, percentile):
    """Nearest-rank percentile of a non-empty sample list, without numpy."""
    ordered = sorted(values)
    rank = math.ceil(percentile / 100.0 * len(ordered))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


def is_measured_duration(value):
    """Whether a caller-supplied duration is a usable positive number of seconds."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def get_chunk_text_length(chunk):
    """Count a chunk's characters; missing or odd text reads as empty."""
    text = chunk.get('text')
    if isinstance(text, str):
        return len(text)
    return 0


def get_chunk_seconds(chunk, durations):
    """Return a chunk's measured duration only when the caller supplied a usable one."""
    if not isinstance(durations, dict):
        return None
    chunk_id = chunk.get('id')
    if not isinstance(chunk_id, (int, str)):
        return None
    seconds = durations.get(chunk_id)
    if is_measured_duration(seconds):
        return seconds
    return None


def get_runtime_totals(chunks, durations):
    """Split chunk rows into measured audio, projected characters and sample rates."""
    sample_rates = []
    measured_seconds = 0.0
    remaining_chars = 0
    done_chunks = 0
    total_chunks = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        total_chunks += 1
        chars = get_chunk_text_length(chunk)
        seconds = get_chunk_seconds(chunk, durations)
        if chunk.get('status') != 'done':
            remaining_chars += chars
            continue
        done_chunks += 1
        if seconds is None:
            remaining_chars += chars
            continue
        measured_seconds += seconds
        if chars > 0:
            sample_rates.append(chars / seconds)
    return {'sample_rates': sample_rates, 'measured_seconds': measured_seconds,
            'remaining_chars': remaining_chars, 'done_chunks': done_chunks,
            'total_chunks': total_chunks}


def get_pause_seconds(chunks):
    """Silence the export puts between consecutive chunks, by the production pause rule."""
    rows = [chunk for chunk in chunks if isinstance(chunk, dict)]
    return sum(get_pause_duration_ms(previous, following)
               for previous, following in zip(rows, rows[1:])) / 1000.0


def get_book_runtime_estimate(chunks, durations):
    """Estimate the finished audiobook's runtime and file sizes (never raises on odd rows).

    Speech is measured or projected per chunk; the pauses between chunks are added by the
    export's own rule. Edge-silence trimming at export is not modelled, so the estimate
    leans slightly long."""
    totals = get_runtime_totals(chunks, durations)
    sample_rates = totals['sample_rates']
    if len(sample_rates) >= 3:
        basis = 'measured'
        rate = get_percentile(sample_rates, 50)
        low_rate = get_percentile(sample_rates, 90)
        high_rate = get_percentile(sample_rates, 10)
    else:
        basis = 'fallback'
        rate = FALLBACK_CHARS_PER_SECOND
        low_rate = FALLBACK_CHARS_PER_SECOND
        high_rate = FALLBACK_CHARS_PER_SECOND
    measured_seconds = totals['measured_seconds'] + get_pause_seconds(chunks)
    remaining_chars = totals['remaining_chars']
    total_seconds = measured_seconds + remaining_chars / rate
    low_seconds = measured_seconds + remaining_chars / low_rate
    high_seconds = measured_seconds + remaining_chars / high_rate
    return {
        'basis': basis,
        'samples': len(sample_rates),
        'chars_per_second': round(rate, 1),
        'total_seconds': round(total_seconds, 1),
        'low_seconds': round(low_seconds, 1),
        'high_seconds': round(high_seconds, 1),
        'done_chunks': totals['done_chunks'],
        'total_chunks': totals['total_chunks'],
        'pause_seconds': round(get_pause_seconds(chunks), 1),
        'wav_bytes': int(round(total_seconds * WAV_BYTES_PER_SECOND)),
        'mp3_bytes': int(round(total_seconds * MP3_BYTES_PER_SECOND)),
    }
