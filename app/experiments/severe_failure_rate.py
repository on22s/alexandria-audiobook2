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
                all `max_new_tokens` is (almost) exactly cap x 1920 samples
                long. Synthesis windows join several generations into one take,
                so the cap applies per window and the rule cannot be used: such
                takes are unmeasured.
    silences    trailing >= 1.2 s and longest internal >= 1.5 s, LoCA's
                thresholds, on takes of any length (the existing acoustic check
                stops at 15 s and sets no threshold).
    the rate    per reason AND the union, each take counted once.

UNMEASURED IS NEVER A PASS. No audio, no current receipt, a windowed take or a
foreign sample rate is reported as unmeasured, separately (Rule 21: a fallback
that returns a plausible value is the dangerous kind).

Usage:
    python app/experiments/severe_failure_rate.py --chunks <book>/chunks.json \
        --audio-root <book dir> --config app/config.json --out report.json
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
CAP_TOLERANCE_TOKENS = 1
TRAILING_SILENCE_SECONDS = 1.2
INTERNAL_SILENCE_SECONDS = 1.5
FRAME_SECONDS = 0.01
REASONS = ("content", "all_silent", "cap_hit", "trailing_silence", "internal_silence")


def get_cap_hit(frames, sample_rate, max_new_tokens, windowed=False):
    """True / False, or None (unmeasured) when the length rule cannot apply."""
    if windowed or sample_rate != CODEC_SAMPLE_RATE or not max_new_tokens:
        return None
    return frames >= (max_new_tokens - CAP_TOLERANCE_TOKENS) * CODEC_UPSAMPLE


def get_silence_flags(samples, rate, threshold_dbfs=None):
    """Leading/trailing/longest-internal silence in seconds, and the flags."""
    import numpy as np

    if threshold_dbfs is None:
        from tts import EDGE_SILENCE_THRESHOLD_DBFS as threshold_dbfs
    values = np.asarray(samples, dtype="float64")
    step = max(1, int(round(rate * FRAME_SECONDS)))
    count = math.ceil(len(values) / step) if len(values) else 0
    padded = np.pad(values, (0, count * step - len(values))).reshape(count, step) if count else values.reshape(0, 1)
    rms = np.sqrt(np.mean(padded * padded, axis=1)) if count else np.zeros(0)
    with np.errstate(divide="ignore"):
        dbfs = 20 * np.log10(rms)
    loud = np.flatnonzero(dbfs > threshold_dbfs)
    if not loud.size:
        return {"all_silent": True, "leading_silence_s": round(count * FRAME_SECONDS, 3),
                "trailing_silence_s": None, "longest_internal_silence_s": None,
                "trailing_silence": False, "internal_silence": False}
    leading = loud[0] * FRAME_SECONDS
    trailing = (count - 1 - loud[-1]) * FRAME_SECONDS
    gaps = np.diff(loud) - 1
    internal = float(gaps.max()) * FRAME_SECONDS if gaps.size else 0.0
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


def get_take_flags(chunk, audio_root, max_new_tokens, windowed):
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
    mono = samples.mean(axis=1)
    silence = get_silence_flags(mono, rate)
    return {"audio": "ok", "duration_s": round(len(mono) / rate, 3), "content_status": content,
            "content": None if content == "unmeasured" else content == "flagged",
            "cap_hit": get_cap_hit(len(mono), rate, max_new_tokens, windowed),
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
    from synthesis_settings import get_enabled_synthesis_window_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", required=True)
    parser.add_argument("--audio-root", required=True)
    parser.add_argument("--config", default=os.path.join(REPO, "app", "config.json"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    tts = json.load(open(args.config, encoding="utf-8")).get("tts", {})
    max_new_tokens = tts.get("max_new_tokens")
    windowed = get_enabled_synthesis_window_settings(tts.get("synthesis_windows")) is not None
    data = json.load(open(args.chunks, encoding="utf-8"))
    chunks = data if isinstance(data, list) else data.get("chunks", [])
    rows = [{"uid": chunk.get("uid"), "audio_path": chunk.get("audio_path"),
             **get_take_flags(chunk, args.audio_root, max_new_tokens, windowed)}
            for chunk in chunks if chunk.get("audio_path")]
    document = {"summary": get_report(rows), "max_new_tokens": max_new_tokens,
                "synthesis_windows_enabled": windowed,
                "thresholds": {"trailing_silence_s": TRAILING_SILENCE_SECONDS,
                               "internal_silence_s": INTERNAL_SILENCE_SECONDS,
                               "cap_tolerance_tokens": CAP_TOLERANCE_TOKENS},
                "takes_without_audio": sum(1 for chunk in chunks if not chunk.get("audio_path")),
                "rows": rows, "provenance": provenance(__file__, args)}
    atomic_json_write(document, args.out)
    print(json.dumps(document["summary"], indent=1))


if __name__ == "__main__":
    main()
