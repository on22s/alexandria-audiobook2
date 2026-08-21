"""Does refining the model's speaker assignment against conversation
constraints recover any of the selection gap?

WHERE THE IDEA COMES FROM. DiLA (KDD '26) augments an LLM with a refinement
step: the model proposes an initial assignment from its semantic understanding,
and a constraint layer then iteratively repairs it against the problem's hard
constraints. Their layer is differentiable and their problem is MaxSAT; the
transferable part is the SHAPE - propose, then repair - not the machinery.

WHY IT MIGHT APPLY HERE. Attribution's failure is not knowledge, it is
SELECTION. Measured on `two_stage_attribution_w3200.json`: the gold speaker is
in the candidate roster for 100% of 2,494 rows, and the model still answers
something else on 857 of them. Every one of those errors is a pick the roster
already contained, which is exactly the condition a repair step addresses.

THE CONSTRAINT TESTED, and only one, deliberately. In a run of consecutive
quotations with no narration between them, a speaker rarely answers themselves.
When the model assigns the same speaker to two adjacent quotes, at most one can
be right, and the alternation prior says the second should be the previous
distinct speaker.

WHY ONLY ONE. A repair pass with several interacting rules that improves the
total tells you nothing about which rule earned it, and this project has a
standing habit of measuring one thing at a time. If alternation moves nothing,
the shape is wrong here and the elaborate version is not worth building.

WHAT WOULD FALSIFY IT. The repair is PAIRED against the model's own output on
identical rows, and reported with McNemar over the discordant pairs. A repair
that fixes as many as it breaks is not an improvement, and the count of each is
printed rather than only the net.
"""
import argparse
import collections
import json
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from experiments.stats import exact_mcnemar  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
# THE HARNESS'S OWN SCORER, not a second one. My first version compared names
# with plain equality and put the baseline at 52.8% where the artifact's own
# `correct` field says 65.6% - a 13-point gap that was entirely alias groups,
# which live in the FIXTURE and not in the artifact's flat roster. Two scorers
# for one question is the drift [[Rule 15]] exists to prevent, and here it also
# meant the experiment was measuring its own bug.
from experiments.scoring import alias_groups, same_speaker  # noqa: E402

INDEX_RE = re.compile(r"-(\d+)$")


def order_key(row):
    """Quote position within its book, from the PDNC id."""
    m = INDEX_RE.search(row.get("id") or "")
    return int(m.group(1)) if m else -1


def canonical(name):
    return (name or "").strip().upper()


def load_alias_groups(paths):
    """Union of every fixture's alias groups, in the harness's own format."""
    merged = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            merged.extend(alias_groups(json.load(handle)))
    return merged


def refine(rows):
    """-> {id: repaired_prediction}, applying alternation only.

    Runs in reading order per book. A quote whose predicted speaker equals the
    immediately preceding quote's predicted speaker is reassigned to the most
    recent DIFFERENT speaker, when one exists in the roster.
    """
    out, changed = {}, 0
    by_book = collections.defaultdict(list)
    for r in rows:
        by_book[(r.get("id") or "").split(":")[0]].append(r)
    for book, items in by_book.items():
        items.sort(key=order_key)
        history = []
        for row in items:
            pred = canonical(row.get("predicted"))
            new = pred
            if history and pred == history[-1]:
                prior = next((h for h in reversed(history[:-1]) if h != pred), None)
                # Only move to a speaker the roster actually offers, or the
                # repair invents a character - the failure mode the roster
                # check exists to prevent.
                roster = {canonical(c).split(" [ALSO:")[0]
                          for c in (row.get("candidates") or [])}
                if prior and prior in roster:
                    new = prior
                    changed += 1
            out[row["id"]] = new
            history.append(new)
    return out, changed


def score(rows, prediction_of, groups):
    n = correct = 0
    for r in rows:
        exp = r.get("expected")
        if not canonical(exp):
            continue
        n += 1
        correct += same_speaker(prediction_of(r), exp, groups)
    return correct, n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifact")
    ap.add_argument("--fixtures", nargs="+", required=True,
                    help="the gold fixtures the artifact was scored against; "
                         "alias groups live there, not in the artifact")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "constraint_refine.json"))
    args = ap.parse_args()

    with open(args.artifact, encoding="utf-8") as handle:
        rows = json.load(handle).get("rows") or []
    if not rows:
        raise SystemExit(f"{args.artifact} has no rows to refine")
    ordered = [r for r in rows if order_key(r) >= 0]
    if len(ordered) < len(rows):
        print(f"  {len(rows) - len(ordered)} rows carry no position and are "
              f"excluded; alternation needs reading order")

    groups = load_alias_groups(args.fixtures)
    repaired, changed = refine(ordered)
    base_of = lambda r: canonical(r.get("predicted"))                 # noqa: E731
    ref_of = lambda r: repaired.get(r["id"], canonical(r.get("predicted")))  # noqa: E731
    base_c, n = score(ordered, base_of, groups)
    ref_c, _ = score(ordered, ref_of, groups)

    fixed = [r for r in ordered
             if not same_speaker(base_of(r), r.get("expected"), groups)
             and same_speaker(ref_of(r), r.get("expected"), groups)]
    broke = [r for r in ordered
             if same_speaker(base_of(r), r.get("expected"), groups)
             and not same_speaker(ref_of(r), r.get("expected"), groups)]
    p, _b, _c = exact_mcnemar(len(fixed), len(broke))

    payload = {
        "scope": "alternation-only repair of the model's speaker assignment, "
                 "paired against its own output on identical rows",
        "artifact": os.path.basename(args.artifact),
        "rows_scored": n, "predictions_changed": changed,
        "baseline_correct": base_c, "baseline_accuracy": round(base_c / n, 4),
        "refined_correct": ref_c, "refined_accuracy": round(ref_c / n, 4),
        "fixed": len(fixed), "broke": len(broke), "mcnemar_p": p,
        "fixed_examples": [r["id"] for r in fixed[:10]],
        "broke_examples": [r["id"] for r in broke[:10]],
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print(f"  rows scored          {n}")
    print(f"  predictions changed  {changed}")
    print(f"  baseline accuracy    {base_c}/{n} = {base_c/n:.1%}")
    print(f"  refined accuracy     {ref_c}/{n} = {ref_c/n:.1%}")
    print(f"  fixed {len(fixed)}   broke {len(broke)}   McNemar p={p:.3e}")
    verdict = ("the repair helps" if len(fixed) > len(broke) and p < 0.05
               else "the repair hurts" if len(broke) > len(fixed) and p < 0.05
               else "no separation - alternation is not the lever here")
    print(f"  -> {verdict}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
