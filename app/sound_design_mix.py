"""Bounded exact-frame sound mixing inside an existing owned export worker.

This does not select a book, approve a mix, or publish media. The caller owns
the dry snapshot, private staging directory and empty destination until return,
and must fence selected book/source identity before publishing. A failed output
is invalid and remains the caller's responsibility, including its quota charge.
"""
from contextlib import ExitStack
import hashlib
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

from audio_pcm_producer import (PCMSourceRange, PCMSourceStore,
    get_pcm_source_shape, get_promoted_pcm_shape, iter_converted_pcm)
from audio_source_io import read_bounded_audio_hash
from audio_take_history import _version
from sound_design_assets import SoundDesignAssetStore, MAX_TOTAL_BYTES
from sound_design_cues import get_sound_design_cue_plan, _require_int


MAX_BLOCK_FRAMES = 8192
MAX_SOURCE_VOICES = 128


def _read_frames(source, first, count):
    width = source.channels * source.sample_width
    if first < 0 or count < 0 or (first + count) * width > source.byte_count:
        raise ValueError('Mix read exceeds its admitted PCM range')
    source.store.stream.seek(source.offset + first * width)
    data = source.store.stream.read(count * width)
    if len(data) != count * width:
        raise ValueError('Mix source has incomplete PCM frames')
    return data


def _get_loop_samples(source, first, count, crossfade, check_active):
    """Normalize positive overlap weights per frame, including >2 cycles."""
    import numpy as np
    native = source.byte_count // source.frame_width
    period = native - crossfade
    positions = np.arange(first, first + count, dtype=np.int64)
    phase, latest = positions % period, positions // period
    values = np.zeros((count, source.channels), dtype=np.float64)
    weights = np.zeros(count, dtype=np.float64)
    samples = np.empty_like(values)
    window = np.empty(count, dtype=np.float64)
    density = min((native + period - 1) // period, int(latest[-1]) + 1)
    for slot in range(density):
        check_active()
        local, cycle = phase + slot * period, latest - slot
        valid = (cycle >= 0) & (local < native)
        if not valid.any():
            continue
        # A block covers either a complete small period or <=2 contiguous runs.
        # Never allocate the gap between two wrapped, far-apart asset positions.
        samples.fill(0)
        if period <= count:
            length = min(period, native - slot * period)
            data = _read_frames(source, slot * period, length)
            table = np.frombuffer(data, dtype='<i4').reshape(-1, source.channels)
            samples[valid] = table[phase[valid]]
        else:
            boundaries = np.flatnonzero(np.diff(phase) < 0) + 1
            for group in np.split(np.arange(count), boundaries):
                group = group[valid[group]]
                if len(group):
                    data = _read_frames(source, int(local[group[0]]), len(group))
                    samples[group] = np.frombuffer(data, dtype='<i4').reshape(-1, source.channels)
        window.fill(1)
        incoming = (cycle > 0) & (local < crossfade)
        outgoing = local >= period
        window[incoming] *= (local[incoming] + 1) / (crossfade + 1)
        window[outgoing] *= (native - local[outgoing]) / (crossfade + 1)
        window[~valid] = 0
        np.multiply(samples, window[:, None], out=samples)
        values += samples
        weights += window
    if not np.isfinite(weights).all() or (weights <= 0).any():
        raise ValueError('Ambience loop has an uncovered or invalid frame')
    return values / weights[:, None] / 2**31


def _ensure_voice_limit(cues, scope, maximum, block_frames, check_active):
    """Use a cheap proof, then exact bounded metadata blocks when necessary."""
    import numpy as np
    events = []
    for cue in cues:
        if cue['density'] > maximum:
            raise ValueError('Ambience loop exceeds the simultaneous source voice limit')
        events.extend(((cue['start'], cue['density']), (cue['end'], -cue['density'])))
    active, peak = 0, 0
    for _, change in sorted(events):
        active += change
        peak = max(peak, active)
    if peak <= maximum:
        return
    # Per-cue peak densities need not coincide. Do not reject a valid schedule
    # just because the conservative proof fails. The shared deadline also bounds
    # this uncommon exact preflight; it runs before conversion or output writes.
    for first in range(scope[0], scope[1], block_frames):
        check_active()
        end = min(first + block_frames, scope[1])
        counts = np.zeros(end - first, dtype=np.int64)
        for cue in cues:
            left, right = max(first, cue['start']), min(end, cue['end'])
            if left >= right:
                continue
            local = np.arange(left - cue['start'], right - cue['start'], dtype=np.int64)
            if cue['crossfade'] is None:
                count = 1
            else:
                period = cue['native'] - cue['crossfade']
                count = local // period + 1 - np.maximum(0, (local - cue['native']) // period + 1)
            counts[left - first:right - first] += count
        if (counts > maximum).any():
            raise ValueError('Mix exceeds the simultaneous source voice limit')


def write_sound_design_pcm(dry_source, destination, versions, book, dialogue, pcm_plan,
                          assets, directory, *, check_active, scope=None,
                          block_frames=MAX_BLOCK_FRAMES, max_staged_bytes=MAX_TOTAL_BYTES,
                          max_source_voices=MAX_SOURCE_VOICES, timeout=300):
    """Write scoped signed little-endian PCM; return facts only after all fences.

    The logical staging limit includes the complete retained dry store, output,
    and unique converted asset prefixes. Canonical asset retention has its own
    store quota. The caller charges any retained failed staging on later runs.
    Ducking uses half-open cue spans, independent of cue gain/fades/silence.
    Fades have exact zero/one endpoints; a one-frame fade silences that frame.
    Integer output uses power-of-two scaling and round-to-nearest, ties-to-even.
    """
    import numpy as np
    _require_int(block_frames, 1, MAX_BLOCK_FRAMES, 'Mix block frames')
    _require_int(max_staged_bytes, 1, MAX_TOTAL_BYTES, 'Mix staging allowance')
    _require_int(max_source_voices, 1, MAX_SOURCE_VOICES, 'Mix source voices')
    if (not callable(check_active) or type(timeout) not in (int, float)
            or not 0 < timeout <= 3600 or not math.isfinite(timeout)):
        raise ValueError('Invalid mix cancellation or deadline')
    deadline = time.monotonic() + timeout
    def check():
        check_active()
        if time.monotonic() >= deadline:
            raise TimeoutError('Sound mix exceeded its processing deadline')
    check()
    plan = get_sound_design_cue_plan(versions, book, dialogue, pcm_plan,
                                    scope=scope, check_active=check)
    if not plan['can_project']:
        raise ValueError('Review stale or unresolved cues before mixing')
    if sys.byteorder != 'little':
        raise ValueError('The existing PCM converter requires a little-endian host')
    layout = (pcm_plan['channels'], pcm_plan['frame_rate'], pcm_plan['sample_width'])
    if (type(dry_source) is not PCMSourceRange or type(dry_source.store) is not PCMSourceStore
            or get_pcm_source_shape(dry_source) != (layout, pcm_plan['frames'])
            or type(dry_source.offset) is not int or dry_source.offset < 0
            or type(dry_source.byte_count) is not int
            or type(dry_source.store.byte_count) is not int
            or dry_source.offset + dry_source.byte_count > dry_source.store.byte_count):
        raise ValueError('Dry snapshot disagrees with the captured PCM plan')
    if type(assets) is not SoundDesignAssetStore:
        raise ValueError('Mixing requires the immutable sound asset store')
    directory = Path(os.path.abspath(directory))
    if (not directory.is_dir() or Path(os.path.realpath(directory)) != directory
            or directory == assets.directory or assets.directory in directory.parents):
        raise ValueError('Mix staging requires a separate canonical directory')
    name = getattr(destination, 'name', None)
    if not isinstance(name, (str, bytes)):
        raise ValueError('Mix output requires a named private staging file')
    target = Path(os.path.abspath(os.fsdecode(name)))
    if (Path(os.path.realpath(target)) != target or directory not in target.parents
            or target == assets.directory or assets.directory in target.parents):
        raise ValueError('Mix output must remain in its private staging directory')
    dry = dry_source.store.stream
    if dry is None or dry.closed:
        raise ValueError('Dry snapshot store is unavailable')
    dry.flush(); destination.flush()
    before, output_before = os.fstat(dry.fileno()), os.fstat(destination.fileno())
    output_path = target.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_size != dry_source.store.byte_count
            or before.st_nlink > 1 or not stat.S_ISREG(output_before.st_mode)
            or output_before.st_size != 0 or output_before.st_nlink > 1
            or not destination.seekable() or not destination.readable() or not destination.writable()
            or destination.tell() != 0 or 'a' in getattr(destination, 'mode', '')
            or not stat.S_ISREG(output_path.st_mode)
            or (output_path.st_dev, output_path.st_ino) != (output_before.st_dev, output_before.st_ino)
            or (before.st_dev, before.st_ino) == (output_before.st_dev, output_before.st_ino)):
        raise ValueError('Mix output must be an empty private file distinct from dry PCM')
    first, end = plan['scope']
    output_bytes = (end - first) * layout[0] * layout[2]
    cues, selected = [], {}
    for document, placement in zip(versions, plan['cues']):
        if placement['status'] != 'current':
            continue
        recipe = document['recipe']; asset = recipe['asset']; identity = asset['asset_id']
        if asset['sample_width'] != 4 or (identity in selected and selected[identity]['facts'] != asset):
            raise ValueError('Selected canonical asset descriptors disagree')
        native = get_promoted_pcm_shape(((asset['channels'], asset['frame_rate'], 4), asset['frames']),
                                        (layout[0], layout[1], 4), reject_impossible_channels=True)[1]
        frames = placement['end_frame'] - placement['start_frame']
        crossfade = recipe['duration']['loop_crossfade_frames']
        required = native if crossfade is not None else min(frames, native)
        row = selected.setdefault(identity, dict(facts=asset, required=0))
        row['required'] = max(row['required'], required)
        density = 1 if crossfade is None else (native + (native - crossfade) - 1) // (native - crossfade)
        cues.append(dict(recipe=recipe, start=placement['start_frame'], end=placement['end_frame'],
                         native=native, crossfade=crossfade, density=density))
    staged = before.st_size + output_bytes + sum(row['required'] * layout[0] * 4 for row in selected.values())
    def dry_fingerprint():
        check()
        if _version(os.fstat(dry.fileno())) != _version(before):
            raise ValueError('Dry PCM changed during mixing')
        result = read_bounded_audio_hash(dry, before.st_size, check)
        if result[1] != before.st_size or _version(os.fstat(dry.fileno())) != _version(before):
            raise ValueError('Dry PCM changed while verifying')
        return result
    written, dry_digest = hashlib.sha256(), hashlib.sha256()
    pre_peak, post_peak = 0., 0.
    with tempfile.TemporaryDirectory(prefix='.sound-mix-', dir=directory) as staging, ExitStack() as leases:
        for identity, row in selected.items():
            stream, facts = leases.enter_context(assets.open_canonical(identity, cancel_check=check))
            if facts != row['facts']:
                raise ValueError('Selected sound asset facts changed')
            row['stream'] = stream
        if staged > max_staged_bytes:
            raise ValueError('Dry snapshot, converted assets and output exceed the mix staging allowance')
        _ensure_voice_limit(cues, plan['scope'], max_source_voices, block_frames, check)
        fingerprint = dry_fingerprint()
        converted_store = leases.enter_context(PCMSourceStore(staging, check))
        for identity, row in selected.items():
            stream, facts = row['stream'], row['facts']
            raw_digest, raw_bytes = hashlib.sha256(), [0]
            def raw_pieces():
                while True:
                    check()
                    data = stream.read(65536)
                    if not data:
                        break
                    raw_bytes[0] += len(data); raw_digest.update(data)
                    if raw_bytes[0] > facts['frames'] * facts['channels'] * 4 or len(data) % (facts['channels'] * 4):
                        raise ValueError('Canonical sound asset has invalid PCM geometry')
                    yield data
            raw = raw_pieces()
            converted = iter_converted_pcm(raw, (facts['channels'], facts['frame_rate'], 4),
                                           (layout[0], layout[1], 4), check)
            def prefix():
                remaining = row['required'] * layout[0] * 4
                for data in converted:
                    piece = data[:remaining]
                    if piece:
                        yield piece
                        remaining -= len(piece)
                    if not remaining:
                        break
            try:
                row['source'] = converted_store.save_pcm(prefix(), (layout[0], layout[1], 4), row['required'])
            finally:
                converted.close()
            # Include every admitted input byte in the hash even for a crop.
            for _ in raw:
                pass
            if raw_bytes[0] != facts['frames'] * facts['channels'] * 4 or raw_digest.hexdigest() != facts['sha256']:
                raise ValueError('Sound asset changed while converting')
        width, channels = layout[2], layout[0]
        scale = 1 << (width * 8 - 1)
        dtype = np.dtype('i1' if width == 1 else '<i' + str(width))
        for start in range(first, end, block_frames):
            check(); stop = min(start + block_frames, end)
            raw = _read_frames(dry_source, start, stop - start)
            dry_digest.update(raw)
            values = np.frombuffer(raw, dtype=dtype).reshape(-1, channels).astype(np.float64) / scale
            duck = np.ones(stop - start, dtype=np.float64)
            active = []
            for cue in cues:
                left, right = max(start, cue['start']), min(stop, cue['end'])
                if left < right:
                    active.append((cue, left, right))
                    section = slice(left - start, right - start)
                    duck[section] = np.minimum(duck[section], 10 ** (cue['recipe']['mix']['duck_db'] / 20))
            values *= duck[:, None]
            for cue, left, right in active:
                check()
                local, count = left - cue['start'], right - left
                source = selected[cue['recipe']['asset']['asset_id']]['source']
                if cue['crossfade'] is None:
                    data = _read_frames(source, local, count)
                    samples = np.frombuffer(data, dtype='<i4').reshape(-1, channels).astype(np.float64) / 2**31
                else:
                    samples = _get_loop_samples(source, local, count, cue['crossfade'], check)
                times = np.arange(local, local + count, dtype=np.int64)
                mix = cue['recipe']['mix']; envelope = np.ones(count, dtype=np.float64)
                duration = cue['end'] - cue['start']
                for length, distance in ((mix['fade_in_frames'], times),
                                         (mix['fade_out_frames'], duration - 1 - times)):
                    if length:
                        envelope *= np.minimum(1., distance / (length - 1)) if length > 1 else (distance != 0)
                values[left - start:right - start] += samples * (envelope * 10 ** (mix['gain_db'] / 20))[:, None]
            if not np.isfinite(values).all():
                raise ValueError('Mix contains non-finite samples')
            pre_peak = max(pre_peak, float(np.abs(values).max()))
            if (values < -1).any() or (values > 1).any():
                raise ValueError('Sound mix clips; lower explicit levels before approval')
            quantized = np.rint(values * scale)
            if (quantized < -scale).any() or (quantized > scale - 1).any():
                raise ValueError('Sound mix clips; lower explicit levels before approval')
            data = quantized.astype(dtype).tobytes()
            if destination.write(data) != len(data):
                raise OSError('Incomplete sound mix output write')
            written.update(data)
            post_peak = max(post_peak, float(np.abs(quantized).max()) / scale)
        check(); destination.flush()
        if os.fstat(destination.fileno()).st_size != output_bytes:
            raise ValueError('Sound mix output length changed')
        current_path = target.lstat()
        if (Path(os.path.realpath(target)) != target or not stat.S_ISREG(current_path.st_mode)
                or (current_path.st_dev, current_path.st_ino) != (output_before.st_dev, output_before.st_ino)):
            raise ValueError('Sound mix output path changed')
        actual = read_bounded_audio_hash(destination, output_bytes, check)
        if actual != (written.hexdigest(), output_bytes) or dry_fingerprint() != fingerprint:
            raise ValueError('Sound mix or dry PCM bytes changed during rendering')
        for row in selected.values():
            if read_bounded_audio_hash(row['stream'], row['facts']['frames'] * row['facts']['channels'] * 4, check)[0] != row['facts']['sha256']:
                raise ValueError('Sound asset changed during mixing')
        check()
    return dict(schema=1, projection_revision=plan['projection_revision'], scope=plan['scope'],
                frames=end - first, frame_rate=layout[1], channels=layout[0], sample_width=layout[2],
                bytes=output_bytes, sha256=written.hexdigest(), dry_scope_sha256=dry_digest.hexdigest(),
                dry_store_sha256=fingerprint[0], logical_staged_bytes=staged,
                prequantization_peak=pre_peak, postquantization_peak=post_peak)
