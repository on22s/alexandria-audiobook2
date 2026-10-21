"""Bounded internal-window assembly; inter-chunk export timing stays separate."""
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import tempfile

from synthesis_windows import get_synthesis_plan_text


@dataclass(frozen=True)
class SynthesisJoinPolicy:
    sentence_gap_ms: int | None = None
    clause_gap_ms: int | None = None
    paragraph_gap_ms: int | None = None
    max_edge_trim_ms: int = 0
    fade_ms: int = 0
    continuation_crossfade_ms: int = 0
    quiet_threshold_db: float | None = None

    def validate(self):
        for value in (self.sentence_gap_ms, self.clause_gap_ms, self.paragraph_gap_ms):
            if value is not None and (type(value) is not int or not 0 <= value <= 5000):
                raise ValueError('Internal gap targets must be integer milliseconds between 0 and 5000')
        for value, maximum in ((self.max_edge_trim_ms, 2000), (self.fade_ms, 50),
                               (self.continuation_crossfade_ms, 50)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError('Internal trim/fade settings exceed their bounded range')
        value = self.quiet_threshold_db
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                  or not -120 <= value <= 0):
            raise ValueError('Internal quiet threshold must be between -120 and 0 dB')


def _check_plan_results(plan, results):
    text = get_synthesis_plan_text(plan)
    if not isinstance(results, (list, tuple)) or len(results) != len(plan.windows):
        raise ValueError('Internal synthesis results have missing or extra windows')
    indexed = {}
    paths = set()
    files = set()
    for item in results:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('Internal synthesis results need an index and private file')
        index, path = item
        if type(index) is not int or not 0 <= index < len(plan.windows) or index in indexed:
            raise ValueError('Internal synthesis result index is duplicated or invalid')
        path = Path(path).resolve()
        identity = os.path.normcase(str(path))
        info = path.stat()
        file_id = (info.st_dev, info.st_ino) if info.st_ino else None
        if identity in paths or file_id is not None and file_id in files:
            raise ValueError('Internal synthesis reuses a window output file')
        paths.add(identity)
        if file_id is not None:
            files.add(file_id)
        indexed[index] = path
    return text, [indexed[index] for index in range(len(plan.windows))]



def get_synthesis_audio_info(path):
    """Reject unsupported headers before any full PCM validation/decoding."""
    import soundfile as sf
    info = sf.info(path)
    if info.channels not in (1, 2) or not 1 <= info.samplerate <= 384000:
        raise ValueError('Internal synthesis supports mono/stereo at rates up to 384000 Hz')
    from audio_streaming import ensure_pcm_shape_fits_riff
    ensure_pcm_shape_fits_riff(((info.channels, info.samplerate, 4), info.frames))
    return info


def get_synthesis_pcm_shapes(metadata, policy):
    """Preflight captured windows in their one final layout before decoding."""
    from audio_pcm_producer import get_promoted_pcm_shape
    from audio_streaming import ensure_pcm_shape_fits_riff, ensure_pcm_wav_header
    layout = (max(info.channels for info in metadata), max(info.samplerate for info in metadata), 4)
    shapes = [get_promoted_pcm_shape(((info.channels, info.samplerate, 4), info.frames), layout,
                                    reject_impossible_channels=True) for info in metadata]
    maximum_gap = max(policy.sentence_gap_ms or 0, policy.clause_gap_ms or 0, policy.paragraph_gap_ms or 0)
    upper_frames = sum(shape[1] for shape in shapes) + round(maximum_gap * layout[1] / 1000) * (len(metadata) - 1)
    ensure_pcm_wav_header(layout, ordinary_pcm=True)
    ensure_pcm_shape_fits_riff((layout, upper_frames))
    return layout, shapes


def validate_synthesis_audio(path, context, expected_text, cancel_check=None):
    from audio_validation import GeneratedAudioCancelled, validate_generated_audio
    from tts import ExportCancelled
    try:
        return validate_generated_audio(path, context, expected_text=expected_text, cancel_check=cancel_check)
    except GeneratedAudioCancelled as error:
        raise ExportCancelled(str(error)) from error


def _stat_version(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _decoded_pcm(path, channels, check):
    import numpy as np
    import soundfile as sf
    from audio_pcm_producer import PCM_BLOCK_BYTES
    from audio_validation import GeneratedAudioError, validate_finite_audio_values
    with sf.SoundFile(path) as reader:
        while len(block := reader.read(max(1, PCM_BLOCK_BYTES // (channels * 4)),
                                       dtype='float64', always_2d=True)):
            check()
            if block.shape[1] != channels:
                raise GeneratedAudioError('Internal window channel shape changed while decoding')
            validate_finite_audio_values(block, 'Internal synthesis window')
            if float(np.max(block)) > 1 or float(np.min(block)) < -1:
                raise GeneratedAudioError('Internal window exceeds the normalized PCM range')
            # Full-scale +1 maps to the greatest signed PCM value; other source
            # integer samples remain exact in the shared 32-bit layout.
            yield np.clip(np.rint(block * 2147483648), -2147483648, 2147483647).astype('int32').tobytes()


def _range(source, start, frames):
    return replace(source, offset=source.offset + start * source.frame_width,
                   byte_count=frames * source.frame_width,
                   duration_ms=round(1000 * frames / source.frame_rate))


def _quiet_edges(source, threshold, check):
    import numpy as np
    from audio_pcm_producer import iter_raw_pcm, PCM_BLOCK_BYTES
    frames = source.byte_count // source.frame_width
    first, last, cursor = None, None, 0
    for raw in iter_raw_pcm(source, PCM_BLOCK_BYTES):
        check()
        values = np.frombuffer(raw, dtype='int32').reshape(-1, source.channels).astype('int64')
        loud = np.flatnonzero(np.max(np.abs(values), axis=1) > threshold)
        if len(loud):
            if first is None:
                first = cursor + int(loud[0])
            last = cursor + int(loud[-1])
        cursor += len(values)
    if cursor != frames:
        raise ValueError('Internal PCM edge scan has missing frames')
    # A quiet utterance is not permission to trim away its whole performance.
    return (0, 0) if first is None else (first, frames - last - 1)


def _read_boundary(source, start, frames, check):
    import numpy as np
    from audio_pcm_producer import iter_raw_pcm, PCM_BLOCK_BYTES
    check()
    raw = b''.join(iter_raw_pcm(_range(source, start, frames), PCM_BLOCK_BYTES))
    check()
    return np.frombuffer(raw, dtype='int32').reshape(-1, source.channels)


def _faded_pcm(source, start, frames, fade_in, fade_out, check):
    import numpy as np
    from audio_pcm_producer import iter_raw_pcm, PCM_BLOCK_BYTES
    cursor = 0
    for raw in iter_raw_pcm(_range(source, start, frames), PCM_BLOCK_BYTES):
        check()
        if not fade_in and not fade_out:
            yield raw
            continue
        values = np.frombuffer(raw, dtype='int32').reshape(-1, source.channels)
        indices = np.arange(cursor, cursor + len(values))
        gain = np.ones(len(values))
        if fade_in:
            gain *= np.clip(indices / max(1, fade_in - 1), 0, 1)
        if fade_out:
            gain *= np.clip((frames - 1 - indices) / max(1, fade_out - 1), 0, 1)
        yield np.rint(values.astype('float64') * gain[:, None]).astype('int32').tobytes()
        cursor += len(values)


def assemble_synthesis_windows(plan, results, output_path, *, policy=None, cancel_check=None):
    """Install one complete parent WAV or retain the prior destination on failure."""
    import numpy as np
    from audio_pcm_producer import PCMSourceStore, PCMProducer, iter_converted_pcm
    from audio_streaming import iter_frame_silence_pcm, write_producer_wav
    from audio_validation import publish_audio_output
    from tts import ensure_audio_export_active, ExportCancelled, EDGE_SILENCE_THRESHOLD_DBFS

    policy = SynthesisJoinPolicy() if policy is None else policy
    if type(policy) is not SynthesisJoinPolicy:
        raise ValueError('Internal joins need an explicit synthesis seam policy')
    policy.validate()
    check = lambda: ensure_audio_export_active(cancel_check)
    check()
    text, paths = _check_plan_results(plan, results)
    output = Path(output_path)
    if os.path.normcase(str(output.resolve())) in {os.path.normcase(str(path)) for path in paths}:
        raise ValueError('Internal final output must not replace one of its input windows')
    metadata = []
    for index, path in enumerate(paths):
        check()
        version = _stat_version(path)
        info = get_synthesis_audio_info(path)
        metadata.append((info, version))
    layout, shapes = get_synthesis_pcm_shapes([info for info, _ in metadata], policy)
    for index, path in enumerate(paths):
        validate_synthesis_audio(path, f'Internal synthesis window {index}', plan.windows[index].text, cancel_check)
    threshold_db = EDGE_SILENCE_THRESHOLD_DBFS if policy.quiet_threshold_db is None else policy.quiet_threshold_db
    threshold = 2147483648 * 10 ** (threshold_db / 20)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.synthesis-join-', dir=output.parent) as directory, \
            PCMSourceStore(directory, check) as store:
        sources, edges = [], []
        for index, (path, (info, version), shape) in enumerate(zip(paths, metadata, shapes)):
            check()
            raw = _decoded_pcm(path, info.channels, check)
            promoted = iter_converted_pcm(raw, (info.channels, info.samplerate, 4), layout, check)
            source = store.save_pcm(promoted, layout, shape[1])
            if _stat_version(path) != version:
                raise ValueError('Internal window output changed while being snapshotted')
            sources.append(source)
            edges.append(_quiet_edges(source, threshold, check))
        frames = [shape[1] for shape in shapes]
        heads, tails, fade_in, fade_out = ([0] * len(sources) for _ in range(4))
        seams = []
        for index, window in enumerate(plan.windows[:-1]):
            check()
            seam = dict(boundary=window.boundary, left_quiet_trim=0, right_quiet_trim=0,
                        added_silence_frames=0, crossfade_frames=0)
            target = {'sentence': policy.sentence_gap_ms, 'clause': policy.clause_gap_ms,
                      'paragraph': policy.paragraph_gap_ms}.get(window.boundary)
            left_room = frames[index] - heads[index] - tails[index]
            right_room = frames[index + 1] - heads[index + 1] - tails[index + 1]
            if window.boundary == 'continuation' and policy.continuation_crossfade_ms:
                overlap = min(round(policy.continuation_crossfade_ms * layout[1] / 1000),
                              max(0, (left_room - 1) // 2), max(0, (right_room - 1) // 2))
                tails[index] += overlap
                heads[index + 1] += overlap
                seam['crossfade_frames'] = overlap
            else:
                if target is not None:
                    quiet_left = min(max(0, edges[index][1] - tails[index]), max(0, left_room - 1))
                    quiet_right = min(max(0, edges[index + 1][0] - heads[index + 1]), max(0, right_room - 1))
                    target_frames = round(target * layout[1] / 1000)
                    excess = max(0, quiet_left + quiet_right - target_frames)
                    cap = round(policy.max_edge_trim_ms * layout[1] / 1000)
                    left = min(excess // 2, quiet_left, cap)
                    right = min(excess - left, quiet_right, cap)
                    left += min(excess - left - right, quiet_left - left, cap - left)
                    tails[index] += left
                    heads[index + 1] += right
                    seam.update(left_quiet_trim=left, right_quiet_trim=right,
                                target_gap_frames=target_frames,
                                added_silence_frames=max(0, target_frames - quiet_left - quiet_right + left + right))
                fade = round(policy.fade_ms * layout[1] / 1000)
                fade_out[index] = min(fade, max(0, (frames[index] - heads[index] - tails[index]) // 2))
                fade_in[index + 1] = min(fade, max(0, (frames[index + 1] - heads[index + 1] - tails[index + 1]) // 2))
            seams.append(seam)
        for index in range(len(sources)):
            remaining = frames[index] - heads[index] - tails[index]
            fade_in[index] = min(fade_in[index], remaining // 2)
            fade_out[index] = min(fade_out[index], remaining // 2)
        expected_frames = sum(frames) - sum(heads) - sum(tails) + sum(
            seam['added_silence_frames'] + seam['crossfade_frames'] for seam in seams)
        staged = Path(directory) / 'complete.wav'
        with PCMProducer(directory, check) as producer:
            for index, source in enumerate(sources):
                count = frames[index] - heads[index] - tails[index]
                producer.append(_faded_pcm(source, heads[index], count, fade_in[index], fade_out[index], check), layout)
                if index == len(seams):
                    continue
                seam = seams[index]
                overlap = seam['crossfade_frames']
                if overlap:
                    left = _read_boundary(source, frames[index] - tails[index], overlap, check)
                    right = _read_boundary(sources[index + 1], 0, overlap, check)
                    phase = np.array([[.5]]) if overlap == 1 else np.linspace(0, 1, overlap)[:, None]
                    mixed = np.rint(left.astype('float64') * (1 - phase) + right.astype('float64') * phase).astype('int32')
                    producer.append((mixed.tobytes(),), layout)
                if seam['added_silence_frames']:
                    producer.append(iter_frame_silence_pcm(seam['added_silence_frames'], layout, cancel_check), layout)
            if producer.get_frame_count() != expected_frames:
                raise ValueError('Internal assembled PCM disagrees with its exact frame accounting')
            with staged.open('wb') as stream:
                write_producer_wav(stream, producer, (layout, expected_frames), cancel_check)
        check()
        validate_synthesis_audio(staged, 'Complete internal synthesis', text, cancel_check)
        check()
        from audio_generation_owner import (AudioGenerationCancelled, get_audio_generation_owner,
                                            ensure_audio_generation_publication)
        try:
            # Exact-owner admission precedes the striped file lock, as it does
            # at Project attachment. Never reenter that nonreentrant owner lock.
            with ensure_audio_generation_publication():
                checker = None if get_audio_generation_owner() is not None else cancel_check
                if not publish_audio_output(staged, output, cancel_check=checker):
                    raise ExportCancelled('Internal synthesis publication cancelled')
        except AudioGenerationCancelled as error:
            raise ExportCancelled('Internal synthesis publication cancelled') from error
    return dict(prepared_text_sha256=plan.text_sha256, window_count=len(sources),
                channels=layout[0], sample_rate=layout[1], sample_width=layout[2],
                input_promoted_frames=sum(frames), output_frames=expected_frames,
                seams=seams, outer_edges_preserved=True)
