"""Bounded PCM WAV writers; unsupported layouts retain the existing builder."""
import os
from pathlib import Path
import tempfile
import wave

from pydub import AudioSegment
from pydub.audio_segment import audioop

from audio_pcm_producer import (PCMProducer, is_bounded_pcm_supported,
                                iter_raw_pcm, iter_default_silence_pcm)

from tts import (ensure_audio_export_active, get_pause_duration_ms,
                 trim_edge_silence)


# Python wave and Pydub's plain-WAV exporter write a 44-byte PCM header:
# the RIFF size field includes the 36 bytes following the first eight bytes.
MAX_RIFF_PCM_BYTES = 0xffffffff - 36


def ensure_riff_pcm_size(data_bytes):
    if data_bytes > MAX_RIFF_PCM_BYTES:
        raise ValueError(f"WAV export requires {data_bytes} PCM bytes; "
                         f"RIFF WAV permits at most {MAX_RIFF_PCM_BYTES}. "
                         "Export shorter sections instead.")


def get_pcm_shape(segment):
    """Return (layout, frames) for ordinary PCM, without copying its data."""
    if not isinstance(segment, AudioSegment):
        return None
    layout = (segment.channels, segment.frame_rate, segment.sample_width)
    if (any(type(value) is not int or value <= 0 for value in layout)
            or layout[2] not in (1, 2, 4)
            or len(segment.raw_data) % (layout[0] * layout[2])):
        return None
    return layout, len(segment.raw_data) // (layout[0] * layout[2])


def get_promoted_pcm_shape(shape, layout):
    source, frames = shape
    if source[0] != layout[0] and source[0] != 1:
        # Pydub cannot promote a multichannel source to more channels.
        return None
    if frames and source[1] != layout[1]:
        frames = ((frames - 1) * layout[1]) // source[1] + 1
    return layout, frames


def get_joined_pcm_shape(left, right):
    """Mirror builder/Pydub promotion, including fresh-state ratecv rounding."""
    if left is None or right is None:
        return None
    layout = tuple(max(a, b) for a, b in zip(left[0], right[0]))
    left = get_promoted_pcm_shape(left, layout)
    right = get_promoted_pcm_shape(right, layout)
    if left is None or right is None:
        return None
    return layout, left[1] + right[1]


def get_silence_pcm_shape(duration_ms):
    # AudioSegment.silent() defaults; zero/negative pauses produce empty PCM.
    return (1, 11025, 2), max(0, int(11025 * (duration_ms / 1000.0)))


def ensure_pcm_shape_fits_riff(shape):
    if shape is not None:
        layout, frames = shape
        ensure_riff_pcm_size(frames * layout[0] * layout[2])


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
    bounded_supported = is_bounded_pcm_supported(segments)
    if len(speakers) != len(segments):
        return None
    segments = [trim_edge_silence(segment, cancel_check=cancel_check)
                for segment in segments]
    shape = get_pcm_shape(segments[0]) if segments else None
    frames = shape[1] if shape is not None else 0
    for index, segment in enumerate(segments[1:], 1):
        ensure_audio_export_active(cancel_check)
        override = pause_overrides[index - 1] if pause_overrides else None
        duration = get_pause_duration_ms(
            {'speaker': speakers[index - 1], 'pause_after': override},
            {'speaker': speakers[index]}, pause_ms, same_speaker_pause_ms)
        if layout is not None:
            frames += (get_silence_frame_count(duration, layout[1])
                       + len(segment.raw_data) // (layout[0] * layout[2]))
        else:
            # Legacy combine appends (gap + segment), then promotes the prefix.
            piece = get_joined_pcm_shape(get_silence_pcm_shape(duration),
                                         get_pcm_shape(segment))
            shape = get_joined_pcm_shape(shape, piece)
    ensure_audio_export_active(cancel_check)
    ensure_pcm_shape_fits_riff((layout, frames) if layout is not None else shape)
    if layout is None:
        if bounded_supported and is_bounded_pcm_supported(segments) and shape is not None:
            return write_spooled_combined_wav(segments, speakers, path, pause_ms,
                                             same_speaker_pause_ms, pause_overrides,
                                             cancel_check)
        return None
    destination = Path(path)
    channels, frame_rate, sample_width = layout
    frames = 0
    with tempfile.TemporaryDirectory(prefix='.stream-wave-', dir=destination.parent) as directory:
        staged = Path(directory) / 'output.wav'
        stream = wave.open(str(staged), 'wb')
        try:
            stream.setnchannels(channels)
            stream.setframerate(frame_rate)
            stream.setsampwidth(sample_width)
            for index, segment in enumerate(segments):
                ensure_audio_export_active(cancel_check)
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
        except BaseException:
            # Discard this staged WAV; preserve the original cancellation or write fault.
            try:
                stream.close()
            except BaseException:
                pass
            raise
        else:
            stream.close()
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
    ensure_audio_export_active(cancel_check)
    layout = get_streamable_layout([segment for segment, _ in entries])
    if layout is None:
        # Preserve valid legacy layouts, but count their final promoted PCM
        # before the caller allocates silence or joins the complete track.
        shape = get_pcm_shape(AudioSegment.empty())
        for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
            piece = (get_pcm_shape(value) if kind == 'audio'
                     else get_silence_pcm_shape(value))
            shape = get_joined_pcm_shape(shape, piece)
        ensure_audio_export_active(cancel_check)
        ensure_pcm_shape_fits_riff(shape)
        if shape is not None and is_bounded_pcm_supported([segment for segment, _ in entries]):
            return shape
        return None
    data_bytes = 0
    for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
        if kind == 'audio':
            data_bytes += len(value.raw_data)
        else:
            data_bytes += get_silence_frame_count(value, layout[1]) * layout[0] * layout[2]
    ensure_audio_export_active(cancel_check)
    ensure_riff_pcm_size(data_bytes)
    return layout, data_bytes // (layout[0] * layout[2])


def write_track_wav(stream, entries, total_duration_ms, plan, cancel_check=None,
                    spool_directory=None):
    """WAV-to-ZIP requires exact nframes because the member is unseekable."""
    if get_streamable_layout([segment for segment, _ in entries]) is None:
        if spool_directory is None:
            raise ValueError('A destination directory is required for PCM spooling')
        # Spool beside the destination, never in a system-temp fallback directory.
        with PCMProducer(spool_directory, lambda: ensure_audio_export_active(cancel_check)) as producer:
            producer.append([], (1, 1, 1))
            for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
                if kind == 'audio':
                    producer.append(iter_raw_pcm(value), (value.channels, value.frame_rate, value.sample_width))
                else:
                    producer.append(iter_default_silence_pcm(value), (1, 11025, 2))
            write_producer_wav(stream, producer, plan, cancel_check)
        return
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


def write_producer_wav(stream, producer, plan, cancel_check=None):
    """Write an exact-size WAV, including Pydub's signed-to-unsigned 8-bit bias."""
    layout, frames = plan
    if producer.layout != layout or producer.get_frame_count() != frames:
        raise ValueError('PCM producer disagrees with the WAV size preflight')
    ensure_pcm_shape_fits_riff(plan)
    wav = wave.open(stream, 'wb')
    try:
        wav.setnchannels(layout[0])
        wav.setframerate(layout[1])
        wav.setsampwidth(layout[2])
        wav.setnframes(frames)
        for data in producer.read():
            ensure_audio_export_active(cancel_check)
            wav.writeframesraw(audioop.bias(data, 1, 128) if layout[2] == 1 else data)
    except BaseException:
        try:
            wav.close()
        except BaseException:
            pass
        raise
    else:
        wav.close()
    ensure_audio_export_active(cancel_check)


def write_spooled_combined_wav(segments, speakers, path, pause_ms,
                              same_speaker_pause_ms, pause_overrides, cancel_check):
    destination = Path(path)
    check = lambda: ensure_audio_export_active(cancel_check)
    with tempfile.TemporaryDirectory(prefix='.stream-wave-', dir=destination.parent) as directory:
        staged = Path(directory) / 'output.wav'
        with PCMProducer(directory, check) as producer:
            first = segments[0]
            producer.append(iter_raw_pcm(first), (first.channels, first.frame_rate, first.sample_width))
            for index, segment in enumerate(segments[1:], 1):
                override = pause_overrides[index - 1] if pause_overrides else None
                duration = get_pause_duration_ms(
                    {'speaker': speakers[index - 1], 'pause_after': override},
                    {'speaker': speakers[index]}, pause_ms, same_speaker_pause_ms)
                # Preserve (gap + segment) before promoting the accumulated prefix.
                with PCMProducer(directory, check) as piece:
                    piece.append(iter_default_silence_pcm(duration), (1, 11025, 2))
                    piece.append(iter_raw_pcm(segment), (segment.channels, segment.frame_rate, segment.sample_width))
                    producer.append(piece.read(), piece.layout)
            frames = producer.get_frame_count()
            layout = producer.layout
            write_producer_wav(str(staged), producer, (layout, frames), cancel_check)
        # All spool/WAV handles must close successfully before publication.
        check()
        os.replace(staged, destination)
    return round(1000 * (frames / layout[1]))
