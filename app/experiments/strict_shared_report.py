"""Print the strict-shared view of existing lora_serving_eval artifacts.

    python experiments/strict_shared_report.py <artifact.json> [...]

Older artifacts (before 2026-09-11) carry only the full `summary`, where an
unanswered row counts as wrong for its arm. This recomputes, from the rows,
the paired comparison restricted to rows BOTH arms answered - the same block
new artifacts write under `strict` - and lists the ids each arm failed on, so
a one-row evaluator asymmetry is visible as one row rather than argued about.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.manifest import strict_shared_summary  # noqa: E402


def main(paths):
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        full = doc.get("summary") or {}
        strict = strict_shared_summary(doc.get("rows") or [])
        print(f"== {os.path.basename(path)}")
        if not strict:
            print("   (not a two-arm long-schema artifact)")
            continue
        for arm in sorted(strict["arms"]):
            f, s = full.get(arm, {}), strict["arms"][arm]
            fa = f.get("accuracy")
            print(f"   {arm:<6} full {f.get('correct')}/{f.get('n')} = "
                  f"{100*fa if fa is not None else float('nan'):.1f}%   "
                  f"strict {s['correct']}/{s['n']} = {100*(s['accuracy'] or 0):.1f}%")
        pr = strict["paired"]
        print(f"   paired (strict) +{pr['improved']}/-{pr['regressed']} of {strict['shared_ids']}  "
              f"p={pr['p']:.3g}" if pr["p"] is not None else "   paired: nothing shared")
        for arm, ids in strict["dropped_ids_by_arm"].items():
            if ids:
                print(f"   {arm} unanswered ({len(ids)}): {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
