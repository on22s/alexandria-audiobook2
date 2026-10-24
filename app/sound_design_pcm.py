"""Exact dry PCM capture from the export's owned, timeline-trimmed sources."""
from pathlib import Path
import math

from audio_pcm_producer import PCMSourceRange, PCMSourceStore, get_pcm_source_shape
from audio_streaming import (save_trimmed_pcm_sources, get_combined_source_plan,
                            ensure_combined_pcm)
from sound_design_cues import _require_int, MAX_DIALOGUE
from tts import ensure_audio_export_active

MAX_CAPTURE_BYTES = 4 * 1024**3
MAX_CLIP_BYTES = 64 * 1024**2


def get_sound_design_pause_metadata(rows, pause_defaults):
    """Validate all dialogue timing before any take is decoded or materialized."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_DIALOGUE:
        raise ValueError('Dry capture requires bounded dialogue rows')
    if (not isinstance(pause_defaults, (tuple, list)) or len(pause_defaults) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not -60000 <= value <= 60000 for value in pause_defaults)):
        raise ValueError('Dry capture requires bounded native pause settings')
    speakers, overrides = [], []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('speaker'), str)
                or (row.get('pause_after') is not None
                    and (type(row['pause_after']) not in (int, float)
                         or not math.isfinite(row['pause_after'])
                         or not -60000 <= row['pause_after'] <= 60000))):
            raise ValueError('Dry capture requires valid dialogue pause metadata')
        speakers.append(row['speaker'])
        overrides.append(row.get('pause_after'))
    return speakers, overrides


def save_sound_design_dry_pcm(sources, rows, segments, directory, pause_defaults, *,
                              cancel_check=None, max_staged_bytes=MAX_CAPTURE_BYTES,
                              max_clip_bytes=MAX_CLIP_BYTES):
    """Use the same final trim and incremental assembler as write_combined_pcm.

    Input segments are owned snapshots produced by the existing export timeline,
    not arbitrary file paths or user-supplied PCM facts. This does not decode
    encoded takes, reserve a task, approve audio, or publish an output. The caller
    must separately budget decoding and retained stages before constructing them.
    Bounds below cover this call's retained store and temporary PCM assembly;
    max_clip_bytes bounds each load_segment materialization, not total Python RSS.
    """
    _require_int(max_staged_bytes,1,MAX_CAPTURE_BYTES,'Dry capture allowance')
    _require_int(max_clip_bytes,1,MAX_CLIP_BYTES,'Dry clip allowance')
    if (type(sources) is not PCMSourceStore or not isinstance(rows,list)
            or not isinstance(segments,list) or not 1 <= len(rows) <= MAX_DIALOGUE
            or len(rows) != len(segments)):
        raise ValueError('Dry capture requires matching owned dialogue sources')
    speakers, overrides = get_sound_design_pause_metadata(rows, pause_defaults)
    directory = Path(directory)
    if not directory.is_dir() or directory.resolve() != directory.absolute():
        raise ValueError('Dry capture requires a canonical staging directory')
    total = 0
    for row,segment in zip(rows,segments):
        ensure_audio_export_active(cancel_check)
        shape = get_pcm_source_shape(segment)
        if (type(segment) is not PCMSourceRange or segment.store is not sources
                or shape is None or segment.byte_count > max_clip_bytes
                or segment.offset < 0 or segment.offset+segment.byte_count > sources.byte_count):
            raise ValueError('Dry capture source exceeds its owned clip allowance')
        total += segment.byte_count
    # Trimming never expands PCM. Check the maximum store growth before loading
    # a clip, then use the actual post-trim clock to bound assembler temporaries.
    if sources.byte_count + total > max_staged_bytes:
        raise ValueError('Dry capture exceeds its staging allowance')
    trimmed = save_trimmed_pcm_sources(segments,cancel_check,sources)
    plan = get_combined_source_plan(trimmed,speakers,*pause_defaults,overrides,cancel_check)
    if plan is None:
        raise ValueError('Dry capture has no native frame plan')
    layout = (plan['channels'],plan['frame_rate'],plan['sample_width'])
    final_bytes = plan['frames'] * layout[0] * layout[2]
    # Prefix input/output, the incoming piece, and the retained final snapshot
    # can coexist. Four full-output envelopes cover those owned PCM files.
    if sources.byte_count + 4 * final_bytes > max_staged_bytes:
        raise ValueError('Dry assembly exceeds its staging allowance')
    with ensure_combined_pcm(trimmed,speakers,directory,*pause_defaults,overrides,cancel_check) as producer:
        if producer.layout != layout or producer.get_frame_count() != plan['frames']:
            raise ValueError('Dry assembler disagrees with its captured frame plan')
        dry = sources.save_pcm(producer.read(),layout,plan['frames'])
    ensure_audio_export_active(cancel_check)
    return dry,plan
