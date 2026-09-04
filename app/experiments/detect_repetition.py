#!/usr/bin/env python3
"""How often does the TTS get stuck repeating itself?

WHY. Goal 3.2 records a looping failure observed ONCE: asked "you think people
would stay in iro...", the model produced "tanji tanji tanji tanji tanji" - 81%
word error from a stuck syllable. One clip of 1,417 was enough to know the
failure is real and not enough to size it, and the goal says a repetition check
over the clips already on disk would size it and has not been run. This is it.

WHY NO METRIC HERE CATCHES IT. A loop fills the right amount of time, so
duration ratios look normal - the observed clip asked 21 words and produced 23,
inside goal 2.3's band. The audio is real and decodable, so 3.2's file check
passes. Timbre and pitch can be perfect while the words are one syllable
repeated. It is only visible in the TEXT.

THE MEASURE. Over a transcript's words, the longest run of one repeated token,
and the share of the transcript taken by the most frequent token. Both are
scale-free and need no reference, so they work on any arm that stored a
transcript. Reported as a distribution, not a pass/fail: the point is to size a
failure, and a threshold invented here would be a guess dressed as a gate.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORD = re.compile(r"[a-z0-9']+")


def longest_run(words):
    """-> length of the longest run of one token repeated back to back."""
    best = run = 0
    prev = None
    for w in words:
        run = run + 1 if w == prev else 1
        prev = w
        best = max(best, run)
    return best


def dominant_share(words):
    """-> share of the transcript taken by its most frequent token."""
    if not words:
        return 0.0
    return collections.Counter(words).most_common(1)[0][1] / len(words)


def measure(doc):
    out = []
    for row in doc.get("rows") or []:
        if not isinstance(row, dict):
            continue
        text = row.get("transcript")
        if not isinstance(text, str) or not text.strip():
            continue
        words = WORD.findall(text.lower())
        if len(words) < 4:
            continue
        out.append({
            "wav": row.get("wav"),
            "words": len(words),
            "longest_run": longest_run(words),
            "dominant_share": round(dominant_share(words), 4),
            "wer": (round((row.get("errors") or 0) / row["words"], 4)
                    if row.get("words") else None),
            "class": row.get("class"),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifacts", nargs="+")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "repetition_scan.json"))
    args = ap.parse_args()

    per_artifact, rows = {}, []
    for pattern in args.artifacts:
        for path in sorted(glob.glob(pattern)):
            try:
                doc = json.load(open(path, encoding="utf-8"))
            except Exception:                                # noqa: BLE001
                continue
            got = measure(doc)
            if not got:
                continue
            per_artifact[os.path.basename(path)] = len(got)
            for g in got:
                g["artifact"] = os.path.basename(path)
            rows.extend(got)
    if not rows:
        raise SystemExit("no clip carried a transcript; nothing to measure")

    runs = sorted(r["longest_run"] for r in rows)
    doc = {
        "note": "Longest back-to-back repeat of one token, and the share taken "
                "by the most frequent token, per transcribed clip. Sizes the "
                "looping failure recorded at 3.2. No threshold is asserted.",
        "clips": len(rows),
        "artifacts": per_artifact,
        "longest_run": {
            "median": runs[len(runs) // 2],
            "p90": runs[int(len(runs) * 0.9)],
            "max": runs[-1],
        },
        "clips_with_a_run_of": {str(k): sum(1 for r in rows if r["longest_run"] >= k)
                                for k in (3, 4, 5, 8)},
        "worst": sorted(rows, key=lambda r: (-r["longest_run"],
                                             -r["dominant_share"]))[:10],
    }
    try:
        sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
        from provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                 # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    print(f"clips with a transcript : {len(rows)}")
    print(f"longest repeated run    : median {doc['longest_run']['median']}, "
          f"p90 {doc['longest_run']['p90']}, max {doc['longest_run']['max']}")
    for k, v in doc["clips_with_a_run_of"].items():
        print(f"  clips repeating a word {k}+ times in a row: {v} "
              f"({v / len(rows) * 100:.2f}%)")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
