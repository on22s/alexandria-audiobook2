"""Do per-line instructs move a CustomVoice around, and does a constant
anchor hold it? The buddies fork's claim, measured with our instrument.

The fork (Finrandojin/alexandria-audiobook, buddies branch, MIT) reports, on
the CustomVoice path: six real per-line instructions on one text moved the
pitch register 206-238 Hz (~2.5 semitones) and duration 13.8-19.8 s; a
constant `character_style` prefix cut the spectral spread by ~30%; timbre
words in a per-line instruct are the identity re-rolled every line. Our
2.8 drift result (no drift over 2,000 lines) was measured on LoRA voices,
so it says nothing about this path - and on this path the product ignores
`character_style` entirely (tts._local_generate_custom uses default_style
only as the empty-instruct fallback).

Four arms, same text, same seed, same CustomVoice speaker; only the instruct
differs:

    as_written   the per-line instructs a real script carries (pass-3 output)
    no_timbre    the same with the lexicon's timbre/identity words removed
    anchored     a constant character-style anchor prefixed to each as_written
    anchor_only  the anchor alone on every line (the fork's `voice_style`)

Per line: ECAPA similarity to an anchor of the arm's own first lines, pitch
median, vocal tract length, duration. Per arm: the spread (std) of pitch in
semitones and of VTL, mean ECAPA-to-anchor, and mean seconds. The fork's
claims predict: as_written has the widest spreads; no_timbre narrower;
anchored and anchor_only narrowest with the highest ECAPA. Lines and
instructs come from a saved script so the instructs are what pass 3 really
wrote, not fabricated.
"""
import argparse
import json
import os
import re
import statistics
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)

from experiments.instruct_lexicon import TIMBRE_TERMS, hits  # noqa: E402
from experiments.voice_drift import ecapa  # noqa: E402

ANCHOR = "A calm, mid-register adult male voice; steady, measured pace."
ARMS = ("as_written", "no_timbre", "anchored", "anchor_only")


def strip_timbre(instruct):
    """Remove the lexicon's timbre/identity terms, tidying the punctuation
    left behind. Never invents; may leave a shorter clause."""
    out = instruct
    for term in hits(instruct, TIMBRE_TERMS):
        out = re.sub(rf"(?i)(?<![a-z]){re.escape(term)}(?![a-z])", "", out)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s*([,;])\s*([,;])", r"\1", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s+([,;.])", r"\1", out).strip(" ,;.")
    return out if re.search(r"[A-Za-z]", out) else "neutral"


def instruct_for(arm, written):
    if arm == "as_written":
        return written
    if arm == "no_timbre":
        return strip_timbre(written)
    if arm == "anchored":
        return f"{ANCHOR} {written}".strip()
    return ANCHOR


def script_lines(path, count, min_chars=40, max_chars=220):
    """Consecutive (text, instruct) pairs from a saved script - one speaker's
    lines, so the identity should be one voice throughout."""
    data = json.load(open(path, encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("entries") or []
    speakers = {}
    for e in entries:
        if e.get("instruct") and min_chars <= len(e.get("text") or "") <= max_chars:
            speakers.setdefault(e.get("speaker"), []).append((e["text"], e["instruct"]))
    speaker, pairs = max(speakers.items(), key=lambda kv: len(kv[1]))
    return speaker, pairs[:count]


def semitones(hz_values):
    import math
    vals = [math.log2(v) * 12 for v in hz_values if v]
    return round(statistics.pstdev(vals), 3) if len(vals) > 1 else None


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--script", required=True, help="saved script whose instructs to use")
    ap.add_argument("--voice", default="Ryan")
    ap.add_argument("--lines", type=int, default=120)
    ap.add_argument("--anchor-lines", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    ap.add_argument("--work", default=os.path.join(REPO, "ab_test_runtime", "custom_voice_instruct_drift"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    speaker, pairs = script_lines(args.script, args.lines)
    if len(pairs) < args.anchor_lines * 3:
        sys.exit(f"only {len(pairs)} usable lines for {speaker}")
    print(f"{speaker}: {len(pairs)} lines from {os.path.basename(args.script)}; "
          f"{sum(1 for _, i in pairs if hits(i, TIMBRE_TERMS))} instructs carry timbre words")

    import importlib.util
    spec = importlib.util.spec_from_file_location("vcv", os.path.join(APP, "experiments", "voice_compare_view.py"))
    vcv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vcv)
    from tts import TTSEngine
    from experiments.generation import render, GenerationFailed
    import soundfile as sf
    import numpy as np
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"), encoding="utf-8")))
    entry = {"type": "custom", "voice": args.voice, "seed": str(args.seed),
             "character_style": "", "default_style": ""}

    doc = {"experiment": "custom_voice_instruct_drift", "script": os.path.basename(args.script),
           "speaker": speaker, "voice": args.voice, "seed": args.seed, "anchor": ANCHOR,
           "lines": len(pairs), "arms": {}, "provenance": provenance(__file__, args)}
    for arm in args.arms:
        wdir = os.path.join(args.work, arm)
        os.makedirs(wdir, exist_ok=True)
        rows, failures = [], 0
        for i, (text, written) in enumerate(pairs):
            instruct = instruct_for(arm, written)
            wav = os.path.join(wdir, f"line_{i:05d}.wav")
            if not os.path.exists(wav):
                try:
                    render(engine, text, instruct, "SPEAKER", {"SPEAKER": entry}, entry, wav)
                except GenerationFailed as exc:
                    failures += 1
                    rows.append({"i": i, "error": str(exc)[:90]})
                    continue
            info = sf.info(wav)
            p = vcv.pitch_stats(wav)
            rows.append({"i": i, "wav": os.path.relpath(wav, REPO), "instruct": instruct,
                         "seconds": round(info.frames / info.samplerate, 3),
                         "f0_median": p.get("f0_median"), "vtl": vcv.vocal_tract_length(wav)})
            if (i + 1) % 20 == 0:
                print(f"  {arm}: {i + 1}/{len(pairs)}  {failures} failed", flush=True)
        ok = [r for r in rows if not r.get("error")]
        chunks, rate = [], None
        for r in ok[:args.anchor_lines]:
            y, sr = sf.read(os.path.join(REPO, r["wav"]), dtype="float32")
            chunks.append(y.mean(axis=1) if y.ndim > 1 else y)
            rate = rate or sr
        anchor_wav = os.path.join(wdir, "_anchor.wav")
        sf.write(anchor_wav, np.concatenate(chunks), rate)
        later = ok[args.anchor_lines:]
        cos, err = ecapa([(anchor_wav, os.path.join(REPO, r["wav"])) for r in later])
        if not err:
            for r, c in zip(later, cos):
                r["ecapa_vs_anchor"] = c
        scored = [r for r in later if r.get("ecapa_vs_anchor") is not None]
        summary = {
            "generated": len(ok), "failed": failures, "ecapa_error": err,
            "ecapa_mean": round(statistics.mean(r["ecapa_vs_anchor"] for r in scored), 4) if scored else None,
            "ecapa_p10": round(sorted(r["ecapa_vs_anchor"] for r in scored)[len(scored) // 10], 4) if scored else None,
            "f0_median_hz": round(statistics.median(r["f0_median"] for r in ok if r["f0_median"]), 1),
            "f0_spread_semitones": semitones([r["f0_median"] for r in ok]),
            "vtl_spread": round(statistics.pstdev([r["vtl"] for r in ok if r["vtl"]]), 4),
            "seconds_mean": round(statistics.mean(r["seconds"] for r in ok), 3),
            "seconds_per_char": round(sum(r["seconds"] for r in ok) / sum(len(t) for t, _ in pairs[:len(ok)]), 5),
        }
        doc["arms"][arm] = {"summary": summary, "rows": rows}
        print(f"\n{arm}: ecapa {summary['ecapa_mean']}  f0 {summary['f0_median_hz']} Hz spread "
              f"{summary['f0_spread_semitones']} st  vtl spread {summary['vtl_spread']}  "
              f"{summary['seconds_mean']} s/line\n", flush=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)


if __name__ == "__main__":
    main()
