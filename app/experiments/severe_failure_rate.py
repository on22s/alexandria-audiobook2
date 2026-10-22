"""How many of a book's takes are severe failures, and why? (#1520)

LoCA (arXiv:2610.28035, section 4.2 and Appendix D) counts an utterance as a
severe failure when ANY of: high content error, all-silent output, the
generation cap was hit, a long trailing silence, or a long internal silence.
Its point is that mean error rates hide these.

MOST OF THAT ALREADY EXISTS, from #1434, and is reused rather than rebuilt
(Rule 15):

    content     `speech_verification` checks a finished take against its exact
                submitted text with whisper.cpp and a distinct confirmation
                model. Its receipt's `corroborated_content_difference` is the
                content flag here. LoCA's CER > 0.5 is NOT reimplemented: a
                second ASR path with a different rule would drift.
    silence     what counts as silent is `tts.EDGE_SILENCE_THRESHOLD_DBFS`, the
                threshold export trimming already uses, in 10 ms steps.

WHAT THIS ADDS is the three things nothing recorded:

    cap hit     Every generation path saves the model's raw output through
                `TTSEngine._save_wav`; nothing trims at save time. The codec
                decodes 1920 samples per token at 24 kHz, so a take that used
                all `max_new_tokens` has a known length. MEASURED 2026-10-22
                (three lines forced to cap 40 on the RX 9070 XT): each came
                out at exactly 74,880 frames = 39 x 1920, ONE TOKEN SHORT of
                the cap, so a strict cap x 1920 rule misses every hit. The
                same lines at cap 2048 were whole token counts (113, 105,
                105) and not flagged. Synthesis windows join several generations into one take,
                so the cap applies per window and the rule cannot be used: such
                takes are unmeasured. This is a calibration rule only: selected
                takes do not retain a proven original cap/backend for this
                calculation. The book report therefore leaves cap_hit unmeasured,
                regardless of today's config. Capturing generation-time evidence
                is follow-up work under #1520.
    silences    trailing >= 1.2 s and longest internal >= 1.5 s, LoCA's
                thresholds, on takes of any length (the existing acoustic check
                stops at 15 s and sets no threshold).
    the rate    per reason AND the union, each take counted once.

UNMEASURED IS NEVER A PASS. Missing audio or content evidence, and every
unproven generation cap, are reported as unmeasured, separately (Rule 21: a fallback
that returns a plausible value is the dangerous kind).

Usage:
    python app/experiments/severe_failure_rate.py --chunks <book>/chunks.json \
        --audio-root <book dir> --out report.json
"""
import argparse
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import atomic_json_write

CODEC_SAMPLE_RATE = 24000
CODEC_UPSAMPLE = 1920
CAP_TOLERANCE_TOKENS = 1  # a real hit lands at cap - 1 tokens (measured, see docstring)
TRAILING_SILENCE_SECONDS = 1.2
INTERNAL_SILENCE_SECONDS = 1.5
FRAME_SECONDS = 0.01
REASONS = ("content", "all_silent", "cap_hit", "trailing_silence", "internal_silence")


def get_cap_hit(frames, sample_rate, max_new_tokens, windowed=False):
    """Calibration only; caller must already know the original generation context."""
    if (windowed or sample_rate != CODEC_SAMPLE_RATE
            or type(max_new_tokens) is not int or max_new_tokens <= 1):
        return None
    return frames >= (max_new_tokens - CAP_TOLERANCE_TOKENS) * CODEC_UPSAMPLE


def get_silence_flags(samples, rate, threshold_dbfs=None):
    """Leading/trailing/longest-internal silence in seconds, and the flags."""
    import numpy as np

    if threshold_dbfs is None:
        from tts import EDGE_SILENCE_THRESHOLD_DBFS as threshold_dbfs
    values = np.asarray(samples, dtype="float64")
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[1] not in (1, 2):
        raise ValueError("Silence analysis requires mono or stereo frames")
    step = max(1, int(round(rate * FRAME_SECONDS)))
    count = math.ceil(len(values) / step) if len(values) else 0
    # A bin is quiet only when every channel is quiet. Summing waveforms can
    # cancel opposite-polarity channels; averaging powers can hide a quiet voice
    # that is present on just one channel near the threshold.
    # The final bin uses only real samples: zero padding changes its level.
    full = len(values) // step
    rms = np.empty(count)
    if full:
        blocks = values[:full * step].reshape(full, step, values.shape[1])
        rms[:full] = np.sqrt(np.mean(blocks * blocks, axis=1)).max(axis=1)
    if count > full:
        rms[-1] = np.sqrt(np.mean(values[full * step:] ** 2, axis=0)).max()
    with np.errstate(divide="ignore"):
        dbfs = 20 * np.log10(rms)
    loud = np.flatnonzero(dbfs > threshold_dbfs)
    if not loud.size:
        return {"all_silent": True, "leading_silence_s": round(len(values) / rate, 3),
                "trailing_silence_s": None, "longest_internal_silence_s": None,
                "trailing_silence": False, "internal_silence": False}
    # Plain Python numbers: numpy scalars are not JSON-serializable, and the
    # report is written as JSON.
    leading = int(loud[0]) * step / rate
    trailing = (len(values) - min((int(loud[-1]) + 1) * step, len(values))) / rate
    gaps = np.diff(loud) - 1
    internal = int(gaps.max()) * step / rate if gaps.size else 0.0
    return {"all_silent": False, "leading_silence_s": round(leading, 3),
            "trailing_silence_s": round(trailing, 3), "longest_internal_silence_s": round(internal, 3),
            "trailing_silence": trailing >= TRAILING_SILENCE_SECONDS,
            "internal_silence": internal >= INTERNAL_SILENCE_SECONDS}


def get_content_flag(chunk, resolve_audio):
    """'flagged' / 'clean' / 'review' / 'unmeasured' from the existing receipt."""
    from speech_verification_receipt import get_current_speech_verification_receipt

    receipt = get_current_speech_verification_receipt(chunk, resolve_audio=resolve_audio)
    if receipt is None or receipt["status"] == "unavailable":
        return "unmeasured"
    if receipt["status"] == "corroborated_content_difference":
        return "flagged"
    if receipt["status"] == "no_content_candidate":
        return "clean"
    return "review"


def get_take_flags(chunk, audio_root):
    """Every reason for one take: True, False, or None (unmeasured)."""
    import soundfile

    path = chunk.get("audio_path")
    if not path:
        return {"audio": "missing", **{reason: None for reason in REASONS}}
    full = path if os.path.isabs(path) else os.path.join(audio_root, path)
    content = get_content_flag(chunk, lambda p: p if os.path.isabs(p) else os.path.join(audio_root, p))
    try:
        samples, rate = soundfile.read(full, dtype="float64", always_2d=True)
    except (OSError, RuntimeError) as error:
        return {"audio": f"unreadable: {type(error).__name__}", **{reason: None for reason in REASONS}}
    silence = get_silence_flags(samples, rate)
    return {"audio": "ok", "duration_s": round(len(samples) / rate, 3), "content_status": content,
            "content": None if content == "unmeasured" else content == "flagged",
            "cap_hit": None, "cap_status": "unmeasured: original generation cap/backend not proven",
            **silence}


def get_report(rows):
    """Per-reason counts, unmeasured counts and the union rate (each take once)."""
    per_reason = {reason: sum(1 for r in rows if r.get(reason) is True) for reason in REASONS}
    unmeasured = {reason: sum(1 for r in rows if r.get(reason) is None) for reason in REASONS}
    severe = [r for r in rows if any(r.get(reason) is True for reason in REASONS)]
    return {"takes": len(rows), "severe": len(severe),
            "severe_rate": round(len(severe) / len(rows), 4) if rows else None,
            "per_reason": per_reason, "unmeasured": unmeasured,
            "content_review": sum(1 for r in rows if r.get("content_status") == "review")}


def main():
    from experiments.provenance import provenance

    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", required=True)
    parser.add_argument("--audio-root", required=True)
    parser.add_argument("--config", help="Legacy option; current settings never prove a take's original cap")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.chunks, encoding="utf-8") as handle:
        data = json.load(handle)
    chunks = data if isinstance(data, list) else data.get("chunks", [])
    rows = [{"uid": chunk.get("uid"), "audio_path": chunk.get("audio_path"),
             **get_take_flags(chunk, args.audio_root)}
            for chunk in chunks if chunk.get("audio_path")]
    document = {"summary": get_report(rows),
                "metric_scope": "selected takes; partial coverage, not the full LoCA evaluation metric",
                "cap_status": "unmeasured: generation-time capture required; current config is not evidence",
                "thresholds": {"trailing_silence_s": TRAILING_SILENCE_SECONDS,
                               "internal_silence_s": INTERNAL_SILENCE_SECONDS,
                               "cap_tolerance_tokens": CAP_TOLERANCE_TOKENS},
                "takes_without_audio": sum(1 for chunk in chunks if not chunk.get("audio_path")),
                "rows": rows, "provenance": provenance(__file__, args)}
    atomic_json_write(document, args.out)
    print(json.dumps(document["summary"], indent=1))


if __name__ == "__main__":
    main()
