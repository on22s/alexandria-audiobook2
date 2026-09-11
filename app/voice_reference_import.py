"""Gate on voice-reference (clone) imports: normalise, measure, refuse, record.

A clone reference is the whole identity of a cloned voice, and until
2026-09-11 the only check on an upload was its file extension. A silent,
clipped, or 1-second clip cloned fine and produced a bad voice nobody could
explain, and nothing recorded where the audio came from or whether we may use
it. This module does what the Qwen3-TTS reference path actually needs
(24 kHz mono PCM16 WAV, one clean 3-30 s sentence), measures the result, and
turns the measurements into named problems. The route refuses on any problem;
the manifest keeps the measurements and the provenance beside the file.

Thresholds, with the reason each exists:
- MIN_SECONDS 3.0 / MAX_SECONDS 30.0: the engine prompt window; train_lora's
  own reference cap is 30 s, and under 3 s there is not one full sentence.
- MAX_CLIPPING_RATIO 0.01: >1 % of samples at full scale is a clipped
  recording, and the timbre the model copies is the distortion.
- MAX_SILENCE_RATIO 0.5: a clip that is mostly silence tells the model little
  and the ICL prompt is mostly padding.
- MIN_PEAK 0.05: below -26 dBFS peak the clip is effectively empty.
"""
import hashlib
import os

import numpy as np
import soundfile as sf

TARGET_RATE = 24000          # what tts.py writes and reads; see _save_wav
MIN_SECONDS = 3.0
MAX_SECONDS = 30.0
MAX_CLIPPING_RATIO = 0.01
MAX_SILENCE_RATIO = 0.5
MIN_PEAK = 0.05


def normalize_reference_audio(src, dst):
    """Decode any container (pydub/ffmpeg) and write 24 kHz mono 16-bit WAV.

    Raises ValueError when the input does not decode as audio."""
    from pydub import AudioSegment
    try:
        audio = AudioSegment.from_file(src)
    except Exception as exc:                                # noqa: BLE001
        raise ValueError(f"could not decode audio: {exc}") from exc
    audio = audio.set_channels(1).set_frame_rate(TARGET_RATE).set_sample_width(2)
    audio.export(dst, format="wav")
    return dst


def measure_reference_audio(path):
    """-> {duration_s, sample_rate, peak, silence_ratio, clipping_ratio, sha256}
    for a normalised WAV. Measurements only; judging them is check_reference_audio."""
    samples, rate = sf.read(path, dtype="float32", always_2d=True)
    mono = samples.mean(axis=1) if samples.shape[1] > 1 else samples[:, 0]
    n = int(mono.size)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "duration_s": round(n / rate, 3) if rate else 0.0,
        "sample_rate": int(rate),
        "peak": float(np.max(np.abs(mono))) if n else 0.0,
        "silence_ratio": float(np.mean(np.abs(mono) < 0.001)) if n else 1.0,
        "clipping_ratio": float(np.mean(np.abs(mono) >= 0.999)) if n else 0.0,
        "sha256": digest.hexdigest(),
    }


def check_reference_audio(measures):
    """-> list of problems (empty = usable). Every violated rule is named, so
    a refusal says what to fix rather than that something is wrong."""
    problems = []
    d = measures["duration_s"]
    if d < MIN_SECONDS:
        problems.append(f"too short: {d:.1f}s (need at least {MIN_SECONDS:.0f}s, one complete sentence)")
    if d > MAX_SECONDS:
        problems.append(f"too long: {d:.1f}s (at most {MAX_SECONDS:.0f}s; trim to one sentence)")
    if measures["peak"] < MIN_PEAK:
        problems.append(f"near-empty audio: peak {measures['peak']:.3f} (below {MIN_PEAK})")
    if measures["clipping_ratio"] > MAX_CLIPPING_RATIO:
        problems.append(f"clipped: {100*measures['clipping_ratio']:.1f}% of samples at full scale")
    if measures["silence_ratio"] > MAX_SILENCE_RATIO:
        problems.append(f"mostly silence: {100*measures['silence_ratio']:.0f}% of samples silent")
    return problems


def import_reference_audio(src, dst):
    """Normalise src into dst, measure, and return (measures, problems).
    On a decode failure returns ({}, [reason]) and writes nothing."""
    try:
        normalize_reference_audio(src, dst)
    except ValueError as exc:
        if os.path.exists(dst):
            os.remove(dst)
        return {}, [str(exc)]
    measures = measure_reference_audio(dst)
    return measures, check_reference_audio(measures)
