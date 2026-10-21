"""Exact source-frame geometry for the existing incremental PCM assembler."""
import math

from audio_pcm_producer import (get_joined_pcm_shape, get_promoted_pcm_shape,
                                get_silence_pcm_shape)

MAX_PLAN_REGIONS = 100000


def _get_promoted_frame_ranges(pauses, shape, layout, check_active, *, preserve_empty=False):
    if shape[0][1] == layout[1]:
        return pauses
    old_rate, rate = shape[0][1], layout[1]
    frames = get_promoted_pcm_shape(shape, layout, reject_impossible_channels=True)[1]
    promoted = []
    for start, end in pauses:
        if check_active is not None:
            check_active()
        if preserve_empty and start == end:
            boundary = get_promoted_pcm_shape((shape[0], start), layout,
                reject_impossible_channels=True)[1]
            promoted.append([boundary, boundary])
            continue
        # Include interpolation edges influenced by these source samples. ratecv
        # starts at source sample zero, and the final output stops at the last
        # available source sample rather than adding an extrapolated frame.
        start = 0 if start == 0 else ((start - 1) * rate) // old_rate + 1
        end = min(frames, (end * rate + old_rate - 1) // old_rate)
        if end > start:
            promoted.append([start, end])
    return promoted


def get_combined_pcm_plan(shapes, pause_durations, check_active=None):
    """Plan trimmed sources using the writer's `(gap + source)` promotion order.

    Pauses are integer-frame envelopes, including resampler interpolation
    edges. No source samples are read and no caller-owned collection changes.
    """
    if len(pause_durations) != max(0, len(shapes) - 1):
        raise ValueError('PCM planning requires one pause between each source.')
    if len(pause_durations) > MAX_PLAN_REGIONS:
        raise ValueError('Export PCM plan exceeds its region limit.')
    if not shapes or any(shape is None for shape in shapes):
        return None
    for layout, frames in shapes:
        if (len(layout) != 3 or any(type(value) is not int or value <= 0 for value in layout)
                or layout[2] not in (1, 2, 4) or type(frames) is not int or frames < 0):
            raise ValueError('Invalid source PCM shape for export planning.')
    shape, pauses = shapes[0], []
    source_ranges = [[0, shape[1]]]
    for source, duration in zip(shapes[1:], pause_durations):
        if check_active is not None:
            check_active()
        if not isinstance(duration, (int, float)) or not math.isfinite(duration):
            raise ValueError('Invalid authored PCM pause.')
        silence = get_silence_pcm_shape(duration)
        piece = get_joined_pcm_shape(silence, source, reject_impossible_channels=True)
        gap_frames = get_promoted_pcm_shape(silence, piece[0], reject_impossible_channels=True)[1]
        final = get_joined_pcm_shape(shape, piece, reject_impossible_channels=True)
        pauses = _get_promoted_frame_ranges(pauses, shape, final[0], check_active)
        source_ranges = _get_promoted_frame_ranges(source_ranges, shape, final[0], check_active, preserve_empty=True)
        offset = get_promoted_pcm_shape(shape, final[0], reject_impossible_channels=True)[1]
        incoming = _get_promoted_frame_ranges([[0, gap_frames]] if gap_frames else [],
                                              piece, final[0], check_active)
        pauses.extend([[offset + start, offset + end] for start, end in incoming])
        incoming_source = _get_promoted_frame_ranges([[gap_frames, piece[1]]], piece, final[0],
                                                     check_active, preserve_empty=True)[0]
        source_ranges.append([offset + boundary for boundary in incoming_source])
        shape = final
    layout, frames = shape
    return dict(frames=frames, frame_rate=layout[1], channels=layout[0], sample_width=layout[2],
                intentional_pause_frames=pauses, source_frame_ranges=source_ranges)
