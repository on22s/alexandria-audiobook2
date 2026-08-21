"""Is anchor length still a live variable, or is it already controlled?

Goal 2.2 records clip length as "the whole cause" of the Chinese anchor sitting
below its own arms, fixed 2026-08-06. That is correct about the failure it
describes. The risk is the sentence travelling further than the evidence: into
"the Chinese cell reads low because the clips are short", and from there into
spending a card on lengthening anchors.

This tabulates what the score artifacts actually contain - anchor length, test
length, and the ceiling they produce - so the question is settled by reading
rather than by re-running.

ANCHOR LENGTH IS ALREADY CONTROLLED. `ljspeech_score.ANCHOR_MIN_SECONDS` is
7.0 and `build_anchor_side` concatenates consecutive same-speaker clips until
it is met, so every anchor on disk is 8.8-9.4s. Nothing is gained by making
them longer; a plan to do so is a plan to rebuild what already exists.

The two sides are reported separately because they are different clips and only
one of them varies.
"""
import argparse
import glob
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402


def median(values):
    return round(statistics.median(values), 2) if values else None


def audit(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if not isinstance(doc, dict):
        return None
    rows = doc.get("rows") or []
    anchors, tests = [], []
    for row in rows:
        block = row.get("human_vs_human")
        if isinstance(block, dict) and block.get("anchor_seconds"):
            anchors.append(block["anchor_seconds"])
        if row.get("human_seconds"):
            tests.append(row["human_seconds"])
    summary = doc.get("summary")
    if not isinstance(summary, dict) or not anchors:
        return None
    def arm(name):
        block = summary.get(name)
        return block.get("ecapa") if isinstance(block, dict) else None
    ceiling = arm("human_vs_human")
    arms = {k: arm(k) for k in ("clone", "lora") if arm(k) is not None}
    return {
        "artifact": os.path.basename(path),
        "rows": len(rows),
        "anchor_seconds_median": median(anchors),
        "anchor_seconds_min": round(min(anchors), 2),
        "test_seconds_median": median(tests),
        "ceiling": None if ceiling is None else round(ceiling, 4),
        "arms": {k: round(v, 4) for k, v in arms.items()},
        "ceiling_clears_every_arm": (
            None if ceiling is None or not arms
            else all(ceiling >= v for v in arms.values())),
        "closest_arm_gap": (
            None if ceiling is None or not arms
            else round(ceiling - max(arms.values()), 4)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", nargs="+",
                    default=sorted(glob.glob(os.path.join(
                        REPO, "ab_test_runtime", "experiments", "*_score.json"))))
    ap.add_argument("--min-seconds", type=float, default=7.0,
                    help="ljspeech_score.ANCHOR_MIN_SECONDS; anchors below "
                         "this would mean the concatenation is not working")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = [r for r in (audit(p) for p in args.scores) if r]
    if not rows:
        raise SystemExit("no score artifact carried anchor_seconds")
    rows.sort(key=lambda r: r["artifact"])

    short = [r for r in rows if r["anchor_seconds_min"] < args.min_seconds]
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "anchor length, test length and ceiling as they stand in the "
                 "score artifacts; nothing was re-scored",
        "anchor_min_seconds": args.min_seconds,
        "artifacts": rows,
        "artifacts_with_an_anchor_below_the_minimum": [
            r["artifact"] for r in short],
        "verdict": ("anchor length is already controlled: every anchor meets "
                    "the minimum, so lengthening them is not an available lever"
                    if not short else
                    "some anchors fall below the minimum; the concatenation "
                    "is not doing its job and that is the thing to fix"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-34s %7s %7s %8s %8s %6s" % (
        "artifact", "anchor", "test", "ceiling", "best arm", "clears"))
    for row in rows:
        best = max(row["arms"].values()) if row["arms"] else None
        print("%-34s %7.2f %7s %8s %8s %6s" % (
            row["artifact"][:34], row["anchor_seconds_median"],
            row["test_seconds_median"], row["ceiling"],
            "n/a" if best is None else "%.4f" % best,
            row["ceiling_clears_every_arm"]))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
