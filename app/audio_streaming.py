"""Bounded PCM WAV writers; unsupported layouts retain the existing builder."""
import os
from pathlib import Path
import tempfile
import wave

from pydub import AudioSegment
from pydub.audio_segment import audioop

from tts import (ensure_audio_export_active, get_pause_duration_ms,
                 trim_edge_silence)


def get_streamable_layout(segments):
    if not segments or any(type(segment) is not AudioSegment for segment in segments):
        return None
    first = segments[0]
    layout = (first.channels, first.frame_rate, first.sample_width)
    if any(type(value) is not int for value in layout):
        return None
    if first.channels not in (1, 2) or first.sample_width not in (2, 4) or first.frame_rate < 11025:
        return None
    if any(any(type(value) is not int for value in
               (segment.channels, segment.frame_rate, segment.sample_width))
           or (segment.channels, segment.frame_rate, segment.sample_width) != layout
           or len(segment.raw_data) % segment.frame_width for segment in segments):
        return None
    return layout


def iter_silence_pcm(duration_ms, layout, cancel_check=None):
    """Use Pydub's native ratecv, retaining its state across bounded zero blocks."""
    channels, frame_rate, sample_width = layout
    frames = int(11025 * (duration_ms / 1000.0))
    state = None
    while frames > 0:
        ensure_audio_export_active(cancel_check)
        count = min(frames, 16384)
        data = b'\0' * (count * channels * 2)
        if frame_rate != 11025:
            data, state = audioop.ratecv(data, 2, channels, 11025, frame_rate, state)
        if sample_width != 2:
            data = audioop.lin2lin(data, 2, sample_width)
        yield data
        frames -= count


def iter_segment_pcm(segment, cancel_check=None):
    data = memoryview(segment.raw_data)
    for offset in range(0, len(data), 1024 * 1024):
        ensure_audio_export_active(cancel_check)
        yield data[offset:offset + 1024 * 1024]


def write_combined_wav(segments, speakers, path, pause_ms, same_speaker_pause_ms,
                       pause_overrides=None, cancel_check=None):
    """Return rounded duration, or None without writing for a legacy fallback."""
    ensure_audio_export_active(cancel_check)
    layout = get_streamable_layout(segments)
    if layout is None or len(speakers) != len(segments):
        return None
    destination = Path(path)
    channels, frame_rate, sample_width = layout
    frames = 0
    with tempfile.TemporaryDirectory(prefix='.stream-wave-', dir=destination.parent) as directory:
        staged = Path(directory) / 'output.wav'
        with wave.open(str(staged), 'wb') as stream:
            stream.setnchannels(channels)
            stream.setframerate(frame_rate)
            stream.setsampwidth(sample_width)
            for index, segment in enumerate(segments):
                ensure_audio_export_active(cancel_check)
                segment = trim_edge_silence(segment, cancel_check=cancel_check)
                if index:
                    override = pause_overrides[index - 1] if pause_overrides else None
                    duration = get_pause_duration_ms(
                        {'speaker': speakers[index - 1], 'pause_after': override},
                        {'speaker': speakers[index]}, pause_ms, same_speaker_pause_ms)
                    for data in iter_silence_pcm(duration, layout, cancel_check):
                        stream.writeframesraw(data)
                        frames += len(data) // (channels * sample_width)
                for data in iter_segment_pcm(segment, cancel_check):
                    stream.writeframesraw(data)
                    frames += len(data) // (channels * sample_width)
        ensure_audio_export_active(cancel_check)
        os.replace(staged, destination)
    return round(1000 * (frames / frame_rate))


def iter_track_pieces(entries, total_duration_ms, cancel_check=None):
    cursor = 0
    for segment, start_ms in entries:
        ensure_audio_export_active(cancel_check)
        if start_ms > cursor:
            yield 'silence', start_ms - cursor
        yield 'audio', segment
        cursor = start_ms + len(segment)
    if total_duration_ms > cursor:
        yield 'silence', total_duration_ms - cursor


def get_silence_frame_count(duration_ms, frame_rate):
    """Exact fresh-state ratecv output count after Pydub's source-frame rounding."""
    frames = int(11025 * (duration_ms / 1000.0))
    return 0 if frames <= 0 else ((frames - 1) * frame_rate) // 11025 + 1


def get_track_wav_plan(entries, total_duration_ms, cancel_check=None):
    """Count silence frames without generating or resampling their PCM."""
    layout = get_streamable_layout([segment for segment, _ in entries])
    if layout is None:
        return None
    data_bytes = 0
    for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
        if kind == 'audio':
            data_bytes += len(value.raw_data)
        else:
            data_bytes += get_silence_frame_count(value, layout[1]) * layout[0] * layout[2]
    if data_bytes > 0xffffffff - 36:
        return None
    return layout, data_bytes // (layout[0] * layout[2])


def write_track_wav(stream, entries, total_duration_ms, plan, cancel_check=None):
    """WAV-to-ZIP requires exact nframes because the member is unseekable."""
    layout, frames = plan
    channels, frame_rate, sample_width = layout
    wav = wave.open(stream, 'wb')
    try:
        wav.setnchannels(channels)
        wav.setframerate(frame_rate)
        wav.setsampwidth(sample_width)
        wav.setnframes(frames)
        for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
            pieces = (iter_segment_pcm(value, cancel_check) if kind == 'audio'
                      else iter_silence_pcm(value, layout, cancel_check))
            for data in pieces:
                ensure_audio_export_active(cancel_check)
                wav.writeframesraw(data)
    except BaseException:
        # An incomplete unseekable WAV cannot patch its header on close.
        # The caller discards this staged archive; preserve the original fault.
        try:
            wav.close()
        except BaseException:
            pass
        raise
    else:
        wav.close()
    ensure_audio_export_active(cancel_check)
