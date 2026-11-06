"""Optional full-length affine cleanup (gain + DC removal) for clone refs.

Identity by default. No trim, denoise, EQ, highpass, speed, or silence removal.
The candidate is verified by exact recomputation from the normalized original
plus the canonical plan: frame count, rate and channel must match exactly and
every sample must equal the expected PCM16 value within 1 LSB of rounding.
ASR transcript identity and speaker/naturalness are UNMEASURED/UNKNOWN here;
no quality or semantic claim is made. Acceptance still requires the existing
reference admission checks plus explicit transcript/rights/listening confirms.
Implementation identity: reference_cleanup with PLAN_VERSION below; the
candidate file is written as exact PCM16 via soundfile and is verified and
admitted before publication.
"""
import contextlib
import hashlib
import json
import threading

import numpy as np
import soundfile as sf

PLAN_VERSION = "reference-cleanup-v1"
SELECTED_BOUNDS = None
MIN_GAIN = 0.25
MAX_GAIN = 4.0
MAX_COMPARISON_SECONDS = 60.0
IMPLEMENTATION = "reference_cleanup:" + PLAN_VERSION
ENCODER = "soundfile PCM_16"
_SLOTS = threading.BoundedSemaphore(2)
_LOCAL = threading.local()
UNMEASURED = {"asr": "UNMEASURED", "speaker_identity": "UNMEASURED", "naturalness": "UNMEASURED"}


@contextlib.contextmanager
def acquire_cleanup_slot():
    depth = getattr(_LOCAL, "depth", 0)
    if depth:
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth = depth
        return
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("Cleanup is already running; try again shortly.")
    _LOCAL.depth = 1
    try:
        yield
    finally:
        _LOCAL.depth = 0
        _SLOTS.release()


def validate_cleanup_plan(gain, remove_dc):
    if isinstance(gain, bool) or not isinstance(gain, (int, float)):
        raise ValueError("gain must be a number")
    gain = float(gain)
    if not (MIN_GAIN <= gain <= MAX_GAIN):
        raise ValueError(f"gain must be between {MIN_GAIN} and {MAX_GAIN}")
    if not isinstance(remove_dc, bool):
        raise ValueError("remove_dc must be true or false")
    return {"version": PLAN_VERSION, "operations": [
        {"op": "dc_offset_removal", "enabled": remove_dc},
        {"op": "gain", "value": gain},
    ], "selected_bounds": SELECTED_BOUNDS, "bounds_mode": "full-length"}


def canonical_plan(plan):
    return json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_digest(plan):
    return hashlib.sha256(canonical_plan(plan).encode("utf-8")).hexdigest()


def gain_of(plan):
    for operation in plan["operations"]:
        if operation["op"] == "gain":
            return float(operation["value"])
    raise ValueError("plan has no gain operation")


def dc_enabled(plan):
    for operation in plan["operations"]:
        if operation["op"] == "dc_offset_removal":
            return bool(operation["enabled"])
    raise ValueError("plan has no dc operation")


def require_cleanup_plan(plan):
    try:
        expected = validate_cleanup_plan(gain_of(plan), dc_enabled(plan))
        if canonical_plan(plan) != canonical_plan(expected):
            raise ValueError("plan is not canonical")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Invalid full-length cleanup plan.") from error


def _expected_samples(original, gain, remove_dc):
    values = original.astype(np.float64)
    if remove_dc and values.size:
        values = values - float(values.mean())
    scaled = values * gain
    rounded = np.rint(scaled)
    clipped = np.count_nonzero((rounded > 32767.0) | (rounded < -32768.0))
    expected = np.rint(np.clip(scaled, -32768.0, 32767.0)).astype(np.int32)
    return expected, int(clipped)


def _header_info(path):
    info = sf.info(path)
    if info.channels != 1:
        raise ValueError("normalized original must be mono")
    if info.samplerate <= 0 or info.frames < 0:
        raise ValueError("audio header is invalid")
    if info.frames / float(info.samplerate) > MAX_COMPARISON_SECONDS:
        raise ValueError("comparison exceeds 60 seconds")
    return info


def apply_affine_cleanup(original_path, candidate_path, plan):
    require_cleanup_plan(plan)
    with acquire_cleanup_slot():
        info = _header_info(original_path)
        data, rate = sf.read(original_path, dtype="int16", always_2d=False)
        if data.ndim != 1 or int(rate) != int(info.samplerate) or data.size != int(info.frames):
            raise ValueError("normalized original header does not match audio data")
        expected, clipped = _expected_samples(data, gain_of(plan), dc_enabled(plan))
        if clipped:
            raise ValueError(f"gain would clip {clipped} samples; lower the gain")
        sf.write(candidate_path, expected.astype(np.int16), rate, subtype="PCM_16")
        written = _header_info(candidate_path)
        if written.frames != data.size or written.samplerate != int(rate):
            raise ValueError("candidate header does not match the normalized original")
        return candidate_path


def verify_affine_candidate(original_path, candidate_path, plan):
    records = dict(UNMEASURED)
    try:
        require_cleanup_plan(plan)
        slot = acquire_cleanup_slot()
        slot.__enter__()
    except ValueError as error:
        return False, [str(error)], records
    try:
        return _verify_affine_candidate(original_path, candidate_path, plan, records)
    finally:
        slot.__exit__(None, None, None)


def _verify_affine_candidate(original_path, candidate_path, plan, records):
    try:
        original_info = _header_info(original_path)
        candidate_info = _header_info(candidate_path)
    except Exception as error:
        return False, ["candidate could not be read: " + str(error)], records
    if int(original_info.samplerate) != int(candidate_info.samplerate):
        return False, ["sample rate changed"], records
    if int(original_info.frames) != int(candidate_info.frames):
        return False, ["frame count changed"], records
    if int(original_info.frames) == 0:
        return False, ["empty audio: no samples to verify"], records
    try:
        original, original_rate = sf.read(original_path, dtype="int16", always_2d=False)
        candidate, candidate_rate = sf.read(candidate_path, dtype="int16", always_2d=False)
    except Exception as error:
        return False, ["candidate could not be read: " + str(error)], records
    if original.ndim != 1 or candidate.ndim != 1:
        return False, ["original and candidate must be mono"], records
    if int(original_rate) != int(candidate_rate):
        return False, ["sample rate changed"], records
    if original.size != candidate.size:
        return False, ["frame count changed"], records
    if original.size == 0:
        return False, ["empty audio: no samples to verify"], records
    expected, clipped = _expected_samples(original, gain_of(plan), dc_enabled(plan))
    problems = []
    if clipped:
        problems.append(f"clipping: gain would clip {clipped} samples")
    diff = np.abs(candidate.astype(np.int32) - expected)
    changed = int(np.count_nonzero(diff > 1))
    if changed:
        problems.append(f"sample mismatch: {changed} samples differ beyond PCM rounding")
    records["samples_checked"] = int(original.size)
    records["max_abs_difference"] = int(diff.max()) if diff.size else 0
    records["clipped_samples"] = clipped
    return not problems, problems, records


def sha256_file(path, cancelled=None):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if cancelled is not None and cancelled.is_set():
                raise ValueError("Cleanup was cancelled.")
            digest.update(block)
    return digest.hexdigest()
