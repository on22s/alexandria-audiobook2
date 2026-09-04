#!/usr/bin/env python3
"""How long does one second of audio take to generate, per voice path?

WHY THIS EXISTS. Goal 4.1 recorded median 0.91x/0.98x/0.97x and called itself
MET. Those figures do not say which VOICE PATH produced them, and the paths
differ: on 2026-09-04 the same card, the same day and the same validation
clips gave 1.25x through a LoRA and 0.83x through the stock voice. A single
median hides a 50% cost that only appears when an adapter is applied - and the
LoRA path is the one the product ships.

THE UNITS INVERT BETWEEN THE LOG AND THE GOAL, which is the trap here. tts.py
prints `17.9s -> 14.5s audio (0.81x real-time)`, i.e. audio/generation, where
HIGHER is better. Goal 4.1 measures generation/audio, where LOWER is better.
Reading one as the other flips every verdict. This script parses the raw
seconds and reports the goal's convention, never the log's.
"""
import argparse
import json
import os
import re
import statistics as st
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LINE = re.compile(r"done:\s*([0-9.]+)s\s*->\s*([0-9.]+)s audio")
PATH = re.compile(r"TTS \[local (lora|[a-z]+)\]")


def rates(path):
    """-> [generation/audio] for every completed generation in one log."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = LINE.search(line)
            if not m:
                continue
            gen, audio = float(m.group(1)), float(m.group(2))
            if audio > 0:
                out.append(gen / audio)
    return out


def summarise(name, values, target_median=0.90, target_worst=1.50):
    if not values:
        return None
    med, worst = st.median(values), max(values)
    return {
        "log": name,
        "clips": len(values),
        "median_generation_over_audio": round(med, 4),
        "worst": round(worst, 4),
        "best": round(min(values), 4),
        "meets_median_target": med <= target_median,
        "meets_worst_target": worst <= target_worst,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs", nargs="+", help="TTS logs to parse")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "generation_realtime_rate.json"))
    args = ap.parse_args()

    arms = []
    for path in args.logs:
        s = summarise(os.path.basename(path), rates(path))
        if s:
            arms.append(s)
    if not arms:
        raise SystemExit("no completed generations found in any log")

    doc = {"note": "Goal 4.1's metric is generation/audio, LOWER is better. "
                   "The TTS log prints audio/generation; these are inverses "
                   "and reading one as the other flips the verdict.",
           "target_median": 0.90, "target_worst": 1.50, "arms": arms}
    try:
        sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
        from provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=1)
    for a in arms:
        print("%-46s n=%-6d median %.2fx  worst %.2fx  %s"
              % (a["log"][:46], a["clips"], a["median_generation_over_audio"],
                 a["worst"], "MET" if a["meets_median_target"] else "MISSES 4.1"))
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
