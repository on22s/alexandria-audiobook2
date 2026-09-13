"""Does each adapter's clone prompt start and end on silence?

`ref_sample.wav` is the voice-clone prompt every render of an adapter is
conditioned on (tts._ensure_lora_prompt). A blind listener found one that
opens mid-word - the transcript begins "AH ... VAMPIRE" - which no metric here
had counted. This measures the edges of all of them: RMS of the first and last
50 ms relative to the clip's RMS, and whether the transcript's first/last token
looks like a fragment. Reports; decides nothing.

    python experiments/ref_clip_edges.py --out ../ab_test_runtime/experiments/ref_clip_edges.json
"""
import argparse, glob, json, os, re, sys, time

import numpy as np
import soundfile as sf

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EDGE_S = 0.05
# A clip that starts or ends on speech: edge RMS at or above this fraction of
# the whole clip's RMS. Speech onsets in this library sit near 1-2x; silence
# and room tone sit near 0.0-0.1x. 0.5 is the midpoint, stated here so it can
# be argued with.
ABRUPT = 0.5


def edge_ratios(path):
    y, sr = sf.read(path)
    if y.ndim > 1:
        y = y.mean(axis=1)
    n = max(int(EDGE_S * sr), 1)
    rms = lambda a: float(np.sqrt(np.mean(np.square(a)))) if len(a) else 0.0
    body = rms(y) or 1e-9
    return rms(y[:n]) / body, rms(y[-n:]) / body, len(y) / sr


def transcript_edges(text):
    """Crude but honest: a leading lowercase or conjunction, or a trailing
    conjunction/article/preposition, is a fragment."""
    words = re.findall(r"[A-Za-z']+", text or "")
    if not words:
        return {"first": None, "last": None, "starts_fragment": None, "ends_fragment": None}
    first, last = words[0], words[-1]
    tail_words = {"and", "but", "the", "a", "an", "of", "to", "in", "on", "at", "with", "that", "i", "he", "she", "it", "as", "or", "so", "if", "was", "were", "is"}
    return {"first": first, "last": last,
            "starts_fragment": first in {"and", "but", "or", "so"} or (first[0].islower() and first not in {"i"}),
            "ends_fragment": last.lower() in tail_words}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", default=os.path.join(REPO, "lora_models"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = []
    for d in sorted(glob.glob(os.path.join(args.models, "*", ""))):
        name = os.path.basename(d.rstrip("/"))
        wav = os.path.join(d, "ref_sample.wav")
        if not os.path.exists(wav):
            rows.append({"adapter": name, "error": "no ref_sample.wav"})
            continue
        try:
            meta = json.load(open(os.path.join(d, "training_meta.json"), encoding="utf-8"))
        except Exception:
            meta = {}
        head, tail, dur = edge_ratios(wav)
        te = transcript_edges(meta.get("ref_sample_text", ""))
        rows.append({"adapter": name, "duration_s": round(dur, 2),
                     "head_rms_ratio": round(head, 3), "tail_rms_ratio": round(tail, 3),
                     "starts_abrupt": head >= ABRUPT, "ends_abrupt": tail >= ABRUPT,
                     "transcript": te})
    ok = [r for r in rows if "error" not in r]
    summary = {"adapters": len(rows), "measured": len(ok),
               "starts_abrupt": sum(r["starts_abrupt"] for r in ok),
               "ends_abrupt": sum(r["ends_abrupt"] for r in ok),
               "either": sum(r["starts_abrupt"] or r["ends_abrupt"] for r in ok),
               "transcript_starts_fragment": sum(bool(r["transcript"]["starts_fragment"]) for r in ok),
               "transcript_ends_fragment": sum(bool(r["transcript"]["ends_fragment"]) for r in ok),
               "threshold": ABRUPT, "edge_seconds": EDGE_S}
    from experiments.provenance import provenance
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"experiment": "ref_clip_edges", "finished": time.time(),
                   "provenance": provenance(__file__, args),
                   "summary": summary, "rows": rows}, fh, indent=1)
    print(json.dumps(summary, indent=1))
    for r in sorted(ok, key=lambda r: -max(r["head_rms_ratio"], r["tail_rms_ratio"]))[:12]:
        print(f"  {r['adapter']:36} head {r['head_rms_ratio']:.2f} tail {r['tail_rms_ratio']:.2f} "
              f"| {r['transcript']['first']} ... {r['transcript']['last']}")


if __name__ == "__main__":
    main()
