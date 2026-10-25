"""Bounded PCM WAV writers; unsupported layouts retain the existing builder."""
import copy
import os
from contextlib import contextmanager
from pathlib import Path
import struct
import wave

from pydub import AudioSegment
from pydub.audio_segment import audioop

from audio_export_plan import get_combined_pcm_plan
from audio_pcm_producer import (PCMProducer, is_bounded_pcm_supported,
                                iter_raw_pcm, iter_default_silence_pcm, iter_converted_pcm,
                                get_pcm_source_shape, get_pcm_source_byte_count, is_plain_pcm_source,
                                get_promoted_pcm_shape, get_joined_pcm_shape, get_silence_pcm_shape,
                                get_audio_duration_ms)

from tts import (ensure_audio_export_active, get_pause_duration_ms,
                 trim_edge_silence)
from export_staging import create_export_staging


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
    return get_pcm_source_shape(segment)


def is_plain_pcm_sequence(segments):
    """Whether every input has the ordinary native channel-conversion contract."""
    return all(is_plain_pcm_source(segment) and get_pcm_shape(segment) is not None
               for segment in segments)



def ensure_pcm_shape_fits_riff(shape):
    if shape is not None:
        layout, frames = shape
        ensure_riff_pcm_size(frames * layout[0] * layout[2])


def ensure_pcm_wav_header(layout, *, ordinary_pcm):
    """Reject an unrepresentable native WAV header before assembling its PCM."""
    if layout is not None and ordinary_pcm:
        channels, rate, width = layout
        # Match wave.Wave_write's unsigned field order without writing a file.
        struct.pack('<HLLHH', channels, rate, channels * rate * width,
                    channels * width, width * 8)


def get_streamable_layout(segments):
    if not segments or any(not is_plain_pcm_source(segment) for segment in segments):
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
           or get_pcm_source_byte_count(segment) % segment.frame_width for segment in segments):
        return None
    return layout


def iter_silence_pcm(duration_ms, layout, cancel_check=None):
    """Retain silence rounding/state through the shared bounded converter."""
    channels, frame_rate, sample_width = layout
    frames = int(11025 * (duration_ms / 1000.0))
    if frames <= 0:
        return

    def iter_zeros():
        remaining = frames
        while remaining > 0:
            ensure_audio_export_active(cancel_check)
            count = min(remaining, 16384)
            yield b'\0' * (count * channels * 2)
            remaining -= count

    check = lambda: ensure_audio_export_active(cancel_check)
    yield from iter_converted_pcm(iter_zeros(), (channels, 11025, 2), layout, check)


def iter_segment_pcm(segment, cancel_check=None):
    for data in iter_raw_pcm(segment, 1024 * 1024):
        ensure_audio_export_active(cancel_check)
        yield data


def get_combined_source_plan(segments, speakers, pause_ms, same_speaker_pause_ms,
                             pause_overrides, cancel_check):
    """Plan sources that have already passed the assembler's trimming step."""
    pauses = []
    for index in range(1, len(segments)):
        ensure_audio_export_active(cancel_check)
        override = pause_overrides[index - 1] if pause_overrides else None
        pauses.append(get_pause_duration_ms(
            {'speaker': speakers[index - 1], 'pause_after': override},
            {'speaker': speakers[index]}, pause_ms, same_speaker_pause_ms))
    return get_combined_pcm_plan([get_pcm_shape(segment) for segment in segments], pauses,
                                 lambda: ensure_audio_export_active(cancel_check))


def _publish_pcm_plan(plan, layout, frames, callback):
    if callback is None:
        return
    if (plan is None or (plan['channels'], plan['frame_rate'], plan['sample_width']) != layout
            or plan['frames'] != frames):
        raise ValueError('Assembled PCM disagrees with its source-frame plan.')
    callback(copy.deepcopy(plan))


def write_combined_wav(segments, speakers, path, pause_ms, same_speaker_pause_ms,
                       pause_overrides=None, cancel_check=None, source_store=None,
                       pcm_plan_callback=None):
    """Return rounded duration, or None without writing for a legacy fallback."""
    ensure_audio_export_active(cancel_check)
    if len(speakers) != len(segments):
        raise ValueError('Audio segments and speakers must have the same length.')
    layout = get_streamable_layout(segments)
    ordinary_pcm = is_plain_pcm_sequence(segments)
    segments = save_trimmed_pcm_sources(segments, cancel_check, source_store)
    ordinary_pcm = ordinary_pcm and is_plain_pcm_sequence(segments)
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
                       + get_pcm_source_byte_count(segment) // (layout[0] * layout[2]))
        else:
            # Legacy combine appends (gap + segment), then promotes the prefix.
            piece = get_joined_pcm_shape(get_silence_pcm_shape(duration),
                                         get_pcm_shape(segment),
                                         reject_impossible_channels=ordinary_pcm)
            shape = get_joined_pcm_shape(shape, piece,
                                         reject_impossible_channels=ordinary_pcm)
    ensure_audio_export_active(cancel_check)
    final_shape = (layout, frames) if layout is not None else shape
    ensure_pcm_shape_fits_riff(final_shape)
    ensure_pcm_wav_header(final_shape[0] if final_shape is not None else None,
                          ordinary_pcm=ordinary_pcm)
    if layout is None:
        if ordinary_pcm and shape is not None and is_bounded_pcm_supported(segments, shape[0]):
            return write_spooled_combined_wav(segments, speakers, path, pause_ms,
                                             same_speaker_pause_ms, pause_overrides,
                                             cancel_check, pcm_plan_callback=pcm_plan_callback)
        return None
    plan = (get_combined_source_plan(segments, speakers, pause_ms, same_speaker_pause_ms,
                                      pause_overrides, cancel_check)
            if pcm_plan_callback is not None else None)
    destination = Path(path)
    channels, frame_rate, sample_width = layout
    frames = 0
    with create_export_staging(destination.parent, 'wave') as staging:
        directory = staging.path
        staged = directory / 'output.wav'
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
        _publish_pcm_plan(plan, layout, frames, pcm_plan_callback)
        ensure_audio_export_active(cancel_check)
        os.replace(staged, destination)
    return round(1000 * (frames / frame_rate))


def iter_track_pieces(entries, total_duration_ms, cancel_check=None):
    cursor = 0
    shape = get_pcm_shape(AudioSegment.empty())
    for segment, start_ms in entries:
        ensure_audio_export_active(cancel_check)
        if start_ms > cursor:
            gap = start_ms - cursor
            yield 'silence', gap
            shape = get_joined_pcm_shape(shape, get_silence_pcm_shape(gap))
        yield 'audio', segment
        shape = get_joined_pcm_shape(shape, get_pcm_shape(segment))
        cursor = (1000 * shape[1] / shape[0][1]
                  if shape is not None else start_ms + get_audio_duration_ms(segment))
    if total_duration_ms > cursor:
        yield 'silence', total_duration_ms - cursor


def iter_track_frame_pieces(entries, total_duration_ms, layout, cancel_check=None):
    """Align uniform PCM directly to the shared frame clock, without ratecv gaps."""
    cursor = 0
    for segment, start_ms in entries:
        ensure_audio_export_active(cancel_check)
        start = round(start_ms * layout[1] / 1000)
        if start > cursor:
            yield 'silence', start - cursor
            cursor = start
        yield 'audio', segment
        cursor += get_pcm_source_byte_count(segment) // (layout[0] * layout[2])
    remaining = round(total_duration_ms * layout[1] / 1000) - cursor
    if remaining > 0:
        yield 'silence', remaining


def iter_frame_silence_pcm(frames, layout, cancel_check=None):
    frame_width = layout[0] * layout[2]
    while frames > 0:
        ensure_audio_export_active(cancel_check)
        count = min(frames, 16384)
        yield b'\0' * (count * frame_width)
        frames -= count


def get_silence_frame_count(duration_ms, frame_rate):
    """Exact fresh-state ratecv output count after Pydub's source-frame rounding."""
    frames = int(11025 * (duration_ms / 1000.0))
    return 0 if frames <= 0 else ((frames - 1) * frame_rate) // 11025 + 1


def get_track_pcm_plan(entries, total_duration_ms, cancel_check=None, *, legacy=False):
    """Plan the selected direct-frame or legacy-piece track without reading PCM."""
    ensure_audio_export_active(cancel_check)
    layout = None if legacy else get_streamable_layout([segment for segment, _ in entries])
    ordinary_pcm = is_plain_pcm_sequence(segment for segment, _ in entries)
    if layout is None:
        # Preserve valid legacy layouts, but count their final promoted PCM
        # before the caller allocates silence or joins the complete track.
        shape = get_pcm_shape(AudioSegment.empty())
        for kind, value in iter_track_pieces(entries, total_duration_ms, cancel_check):
            piece = (get_pcm_shape(value) if kind == 'audio'
                     else get_silence_pcm_shape(value))
            shape = get_joined_pcm_shape(shape, piece,
                                         reject_impossible_channels=ordinary_pcm)
        ensure_audio_export_active(cancel_check)
        ensure_pcm_shape_fits_riff(shape)
        ensure_pcm_wav_header(shape[0] if shape is not None else None,
                              ordinary_pcm=ordinary_pcm)
        return shape
    data_bytes = 0
    for kind, value in iter_track_frame_pieces(entries, total_duration_ms, layout, cancel_check):
        if kind == 'audio':
            data_bytes += get_pcm_source_byte_count(value)
        else:
            data_bytes += value * layout[0] * layout[2]
    ensure_audio_export_active(cancel_check)
    ensure_riff_pcm_size(data_bytes)
    ensure_pcm_wav_header(layout, ordinary_pcm=ordinary_pcm)
    return layout, data_bytes // (layout[0] * layout[2])


def get_track_wav_plan(entries, total_duration_ms, cancel_check=None):
    """Keep transport eligibility separate from the independent PCM geometry."""
    plan = get_track_pcm_plan(entries, total_duration_ms, cancel_check)
    if plan is not None and is_bounded_pcm_supported(
            [segment for segment, _ in entries], plan[0]):
        return plan
    return None


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
        for kind, value in iter_track_frame_pieces(entries, total_duration_ms, layout, cancel_check):
            pieces = (iter_segment_pcm(value, cancel_check) if kind == 'audio'
                      else iter_frame_silence_pcm(value, layout, cancel_check))
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


@contextmanager
def ensure_combined_pcm(segments, speakers, directory, pause_ms,
                        same_speaker_pause_ms, pause_overrides, cancel_check):
    """Produce the existing (gap + segment) promotion order, one block per write."""
    check = lambda: ensure_audio_export_active(cancel_check)
    with PCMProducer(directory, check) as producer:
        first = segments[0]
        producer.append(iter_raw_pcm(first), (first.channels, first.frame_rate, first.sample_width))
        for index, segment in enumerate(segments[1:], 1):
            override = pause_overrides[index - 1] if pause_overrides else None
            duration = get_pause_duration_ms(
                {'speaker': speakers[index - 1], 'pause_after': override},
                {'speaker': speakers[index]}, pause_ms, same_speaker_pause_ms)
            with PCMProducer(directory, check) as piece:
                piece.append(iter_default_silence_pcm(duration), (1, 11025, 2))
                piece.append(iter_raw_pcm(segment), (segment.channels, segment.frame_rate, segment.sample_width))
                producer.append(piece.read(), piece.layout)
        yield producer


def write_spooled_combined_wav(segments, speakers, path, pause_ms,
                              same_speaker_pause_ms, pause_overrides, cancel_check,
                              pcm_plan_callback=None):
    destination = Path(path)
    plan = (get_combined_source_plan(segments, speakers, pause_ms, same_speaker_pause_ms,
                                      pause_overrides, cancel_check)
            if pcm_plan_callback is not None else None)
    with create_export_staging(destination.parent, 'wave') as staging:
        directory = staging.path
        staged = directory / 'output.wav'
        with ensure_combined_pcm(segments, speakers, directory, pause_ms,
                                 same_speaker_pause_ms, pause_overrides, cancel_check) as producer:
            frames = producer.get_frame_count()
            layout = producer.layout
            write_producer_wav(str(staged), producer, (layout, frames), cancel_check)
        _publish_pcm_plan(plan, layout, frames, pcm_plan_callback)
        ensure_audio_export_active(cancel_check)
        os.replace(staged, destination)
    return round(1000 * (frames / layout[1]))


def save_trimmed_pcm_sources(segments, cancel_check, source_store):
    trimmed_sources = []
    for segment in segments:
        ensure_audio_export_active(cancel_check)
        loaded = source_store.load_segment(segment) if source_store is not None else segment
        trimmed = trim_edge_silence(loaded, cancel_check=cancel_check)
        trimmed_sources.append(source_store.save_segment(trimmed) if source_store is not None else trimmed)
        del segment, loaded, trimmed
    return trimmed_sources


def write_combined_pcm(segments, speakers, path, pause_ms, same_speaker_pause_ms,
                       pause_overrides=None, cancel_check=None, source_store=None,
                       pcm_plan_callback=None):
    """Write a bounded raw PCM stage or return None for the native compatibility path."""
    ensure_audio_export_active(cancel_check)
    if len(speakers) != len(segments):
        raise ValueError('Audio segments and speakers must have the same length.')
    if not is_bounded_pcm_supported(segments):
        return None
    segments = save_trimmed_pcm_sources(segments, cancel_check, source_store)
    if not is_bounded_pcm_supported(segments):
        return None
    plan = (get_combined_source_plan(segments, speakers, pause_ms, same_speaker_pause_ms,
                                      pause_overrides, cancel_check)
            if pcm_plan_callback is not None else None)
    destination = Path(path)
    with create_export_staging(destination.parent, 'pcm') as staging:
        directory = staging.path
        staged = directory / 'output.pcm'
        with ensure_combined_pcm(segments, speakers, directory, pause_ms,
                                 same_speaker_pause_ms, pause_overrides, cancel_check) as producer:
            layout, frames = producer.layout, producer.get_frame_count()
            ensure_pcm_wav_header(layout, ordinary_pcm=True)
            stream = staged.open('wb')
            try:
                for data in producer.read():
                    ensure_audio_export_active(cancel_check)
                    if stream.write(data) != len(data):
                        raise OSError('Incomplete PCM staging write')
            except BaseException:
                try:
                    stream.close()
                except BaseException:
                    pass
                raise
            else:
                stream.close()
        _publish_pcm_plan(plan, layout, frames, pcm_plan_callback)
        ensure_audio_export_active(cancel_check)
        os.replace(staged, destination)
    return {'sample_width': layout[2], 'frame_rate': layout[1], 'channels': layout[0], 'frames': frames}
