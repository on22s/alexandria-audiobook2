"""Does clone similarity track how typical the reference is?

Reads the arms `reference_spread.py` built and the score artifact each one
produced, and reports ECAPA against distance-from-centre. One number decides
it: the correlation between an arm's reference distance and its mean clone
ECAPA, with the per-arm table beside it so a non-monotone result is visible
rather than averaged away.

WHAT A FLAT RESULT MEANS, stated before the run. If ECAPA does not track
distance, reference typicality is not a lever on goal 2.1 and #367's gain came
from the length change alone - which that PR already said it could not
separate. That is a real answer and closes a question, so it is not a failed
experiment.

Spearman rather than Pearson: with four arms the question is whether the order
holds, not whether the relationship is linear, and one outlying arm would
dominate a Pearson fit.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402


def spearman(xs, ys):
    """-> rank correlation, or None when it is not defined.

    Written out rather than imported so this runs in a checkout without scipy,
    which is where the analysis usually gets read.
    """
    n = len(xs)
    if n < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            mean_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = mean_rank
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if not dx or not dy else num / (dx * dy)


def mean_metric(score_path, metric):
    with open(score_path, encoding="utf-8") as handle:
        doc = json.load(handle)
    summary = (doc.get("summary") or {}).get("clone") or {}
    if metric in summary:
        return summary[metric], summary.get("n")
    values = [r["clone"][metric] for r in (doc.get("rows") or [])
              if isinstance(r.get("clone"), dict) and metric in r["clone"]]
    return (sum(values) / len(values), len(values)) if values else (None, 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spread", required=True,
                    help="the manifest reference_spread.py wrote")
    ap.add_argument("--score", nargs="+", required=True,
                    metavar="ARM=FILE", help="e.g. 0=...score_arm0.json")
    ap.add_argument("--metric", default="ecapa")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.spread, encoding="utf-8") as handle:
        spread = json.load(handle)
    by_arm = {a["arm"]: a for a in spread.get("arms") or []}

    rows = []
    for pair in args.score:
        arm, _, path = pair.partition("=")
        if not path:
            raise SystemExit("--score takes ARM=FILE, got %r" % pair)
        arm = int(arm)
        if arm not in by_arm:
            raise SystemExit("arm %d is not in the spread manifest" % arm)
        value, n = mean_metric(path, args.metric)
        rows.append({"arm": arm, "distance": by_arm[arm]["distance"],
                     "ref_seconds": by_arm[arm]["seconds"],
                     "measures": by_arm[arm]["measures"],
                     args.metric: value, "n": n,
                     "score_artifact": os.path.basename(path)})
    rows.sort(key=lambda r: r["distance"])
    usable = [r for r in rows if r[args.metric] is not None]

    rho = spearman([r["distance"] for r in usable],
                   [r[args.metric] for r in usable])
    best = min(usable, key=lambda r: r["distance"]) if usable else None
    worst = max(usable, key=lambda r: r["distance"]) if usable else None
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "clone %s against reference distance from the speaker's own "
                 "centre. Arms share a 10-15s band but not one duration, so "
                 "ref_seconds is reported per arm and a result that tracks it "
                 "instead is not a typicality result" % args.metric,
        "metric": args.metric,
        "arms": rows,
        "spearman_distance_vs_metric": None if rho is None else round(rho, 4),
        "nearest_minus_farthest": None if not (best and worst) else round(
            best[args.metric] - worst[args.metric], 4),
        "corpus_centre": spread.get("corpus_centre"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("arm  distance  ref_s   %s" % args.metric)
    for row in rows:
        print("  %d   %8.5f  %5.1f   %s"
              % (row["arm"], row["distance"], row["ref_seconds"],
                 "n/a" if row[args.metric] is None else "%.4f" % row[args.metric]))
    print("spearman(distance, %s) = %s | nearest - farthest = %s"
          % (args.metric, doc["spearman_distance_vs_metric"],
             doc["nearest_minus_farthest"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
