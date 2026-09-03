"""What do ALL the adapter evaluations say, taken together?

Every arm in this repo is normally read on its own. This reads the whole set at
once and asks two questions the individual artifacts cannot answer:

  1. Do adapters help? Split by base model family.
  2. Is "adapters induce unanswered rows" a universal cost of adapting, or a
     property of the arms that fail?

WHY IT IS A SURVEY AND NOT AN EXPERIMENT. It runs no model and creates no
evidence; it re-reads artifacts that already exist, so it cannot establish
anything those artifacts could not. Its value is that nobody had counted.

WHAT IT DELIBERATELY DOES NOT DO. It does not pool arms into a single "adapters
work" number. The 14B arms are largely the strength ladder - one book at 88
rows, evaluated repeatedly - so they are not independent measurements, and a
pooled mean would read as 13 confirmations when it is closer to one book seen
13 ways. The per-family split with n shown is the honest form.

Prints only; writes no artifact, because a re-reading of existing results is
not an experimental arm and does not belong in the results index.
"""
import argparse
import collections
import glob
import json
import os
import statistics

FAMILIES = ("distill_eval", "lora_serving_eval")


def classify(meta, filename):
    blob = ("%s %s" % (meta.get("model") or "", filename)).lower()
    if "3.8" in blob or "qwen38" in blob:
        return "qwen3.8"
    if "3.5" in blob or "qwen35" in blob:
        return "qwen3.5"
    if "14b" in blob:
        return "qwen3-14b"
    return "other"


def survey(pattern, min_rows):
    out = []
    for path in sorted(glob.glob(pattern)):
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        rows = doc.get("rows")
        meta = doc.get("meta") or {}
        if not isinstance(rows, list) or meta.get("experiment") not in FAMILIES:
            continue
        arms = collections.defaultdict(lambda: {"n": 0, "empty": 0, "correct": 0})
        for row in rows:
            if not isinstance(row, dict):
                continue
            arm = row.get("arm")
            if arm not in ("base", "tuned"):
                continue
            slot = arms[arm]
            slot["n"] += 1
            if row.get("predicted") in (None, "None", ""):
                slot["empty"] += 1
            if row.get("correct"):
                slot["correct"] += 1
        if "base" not in arms or "tuned" not in arms:
            continue
        base, tuned = arms["base"], arms["tuned"]
        if base["n"] < min_rows or tuned["n"] < min_rows:
            continue
        out.append({
            "artifact": os.path.basename(path),
            "family": classify(meta, os.path.basename(path)),
            "rows": base["n"],
            "delta_accuracy": 100.0 * (tuned["correct"] / tuned["n"]
                                       - base["correct"] / base["n"]),
            "delta_empty": 100.0 * (tuned["empty"] / tuned["n"]
                                    - base["empty"] / base["n"]),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiments", default="ab_test_runtime/experiments/*.json")
    ap.add_argument("--min-rows", type=int, default=50,
                    help="skip arms too small to read; 50 excludes the n=10 probes")
    args = ap.parse_args()

    recs = survey(args.experiments, args.min_rows)
    if not recs:
        raise SystemExit("no paired adapter evaluations found")
    print("paired adapter evaluations with >=%d rows per arm: %d\n"
          % (args.min_rows, len(recs)))

    print("%-12s %4s %10s %12s %10s" % ("model", "n", "med d-acc", "med d-empty", "helped"))
    by = collections.defaultdict(list)
    for r in recs:
        by[r["family"]].append(r)
    for fam in sorted(by, key=lambda k: -len(by[k])):
        g = by[fam]
        print("%-12s %4d %+9.1f %+11.1f %8s"
              % (fam, len(g),
                 statistics.median([r["delta_accuracy"] for r in g]),
                 statistics.median([r["delta_empty"] for r in g]),
                 "%d/%d" % (sum(1 for r in g if r["delta_accuracy"] > 0), len(g))))

    helped = [r for r in recs if r["delta_accuracy"] > 0]
    hurt = [r for r in recs if r["delta_accuracy"] <= 0]
    print("\nIS EMPTINESS A COST OF ADAPTING, OR A MARK OF FAILURE?")
    print("  tuned arms with MORE empty rows : %d of %d"
          % (sum(1 for r in recs if r["delta_empty"] > 0), len(recs)))
    print("  tuned arms with FEWER           : %d"
          % sum(1 for r in recs if r["delta_empty"] < 0))
    for label, group in (("arms that helped", helped), ("arms that hurt", hurt)):
        if group:
            print("  %-18s n=%-3d median d-empty %+.1f pp"
                  % (label, len(group),
                     statistics.median([r["delta_empty"] for r in group])))


if __name__ == "__main__":
    main()
