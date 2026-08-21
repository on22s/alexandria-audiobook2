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

THREE CONSTRAINTS, MEASURED ONE AT A TIME. A repair pass with several
interacting rules that improves the total tells you nothing about which rule
earned it, so each is applied alone against the same baseline.

  alternation  a speaker rarely answers themselves. REFUTED 2026-08-21:
               190 fixed against 472 broken, p=1.3e-28. The model gives
               consecutive quotes the same speaker 1,010 times and is right on
               53.9% of them - these novels have long single-speaker runs, so
               the rule overwrites a majority-correct decision.
  roster       a prediction outside the candidate roster is certainly wrong -
               19 rows, all misspellings ("MR. DARYY"). Repairing to the
               nearest roster name can only help or be neutral, but the whole
               population is 0.8% so the ceiling is small.
  adjacency    if the narration just before the quote names exactly one roster
               character, that character is the speaker. This is the rule
               Explicit quotations are DEFINED by, and Explicit is the category
               where this project trails the field most (64.5% against 99.3%).

THE FIRST CONSTRAINT TESTED, and only one at a time, deliberately. In a run of consecutive
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
WORD_RE = re.compile(r"[A-Za-z][A-Za-z.\' -]+")


def load_context(paths):
    """-> {quote_id: (prev_context, next_context)} from the gold fixtures.

    The artifact keeps the line but not what surrounds it; adjacency needs the
    narration. Keyed on the fixture's own id so a row joins to the context that
    produced it rather than to a line that reads the same.
    """
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        for entry in fixture.get("entries") or []:
            out[entry.get("id")] = (entry.get("prev_context") or "",
                                    entry.get("next_context") or "")
    return out


def names_in(text, roster):
    """Roster names appearing in `text`, longest first so "MISS ELIZABETH"
    is not also counted as "ELIZABETH"."""
    upper = (text or "").upper()
    found, taken = [], []
    for name in sorted(roster, key=len, reverse=True):
        if not name:
            continue
        at = upper.find(name)
        if at < 0:
            continue
        if any(at >= s and at < e for s, e in taken):
            continue
        taken.append((at, at + len(name)))
        found.append((at, name))
    return [n for _at, n in sorted(found)]


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


def refine_roster(rows):
    """Repair a prediction the roster does not contain, to its nearest member.

    These are certainly wrong - the answer is not a character in the book - so
    any change is neutral at worst. difflib rather than an edit-distance
    threshold, because the observed failures are single-character slips
    ("MR. DARYY" for "MR. DARCY") and a cutoff tuned on 19 rows would be tuned
    on noise.
    """
    import difflib
    out, changed = {}, 0
    for row in rows:
        pred = canonical(row.get("predicted"))
        roster = [canonical(c) for c in (row.get("candidates") or [])]
        out[row["id"]] = pred
        if roster and pred not in roster:
            near = difflib.get_close_matches(pred, roster, n=1, cutoff=0.8)
            if near:
                out[row["id"]] = near[0]
                changed += 1
    return out, changed


def refine_adjacency(rows, contexts, window=None):
    """Take the roster character named NEAREST before the quote.

    Before, not after: an attribution follows its quote far more often than it
    precedes the next one, and using both halves would let a later speaker's
    introduction claim the earlier line.

    NEAREST, NOT UNIQUE. The first version required exactly one roster name in
    prev_context and fired on 15 of 2,494 rows - the window is 3,200 characters
    and typically holds four or five names, so "unique" is a condition the data
    almost never meets. A rule that fires 15 times has not been tested, it has
    been avoided, and reporting "no separation" on it would have been a
    statement about the rule's rarity dressed up as a result.

    `window` restricts the search to the last N characters, where a speech tag
    actually lives; None searches the whole context and takes the last match.
    """
    out, changed = {}, 0
    for row in rows:
        pred = canonical(row.get("predicted"))
        out[row["id"]] = pred
        quote_id = (row.get("id") or "").split(":", 1)[-1]
        prev, _next = contexts.get(quote_id, ("", ""))
        if not prev:
            continue
        haystack = prev[-window:] if window else prev
        roster = {canonical(c) for c in (row.get("candidates") or [])}
        named = names_in(haystack, roster)
        if named and named[-1] != pred:
            out[row["id"]] = named[-1]
            changed += 1
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
    ap.add_argument("--constraint", action="append",
                    choices=["alternation", "roster", "adjacency",
                             "adjacency_120", "adjacency_400"],
                    help="test one constraint. Repeatable. Default: all "
                         "three, each applied ALONE.")
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
    contexts = load_context(args.fixtures)
    strategies = {
        "alternation": lambda rows: refine(rows),
        "roster": lambda rows: refine_roster(rows),
        "adjacency": lambda rows: refine_adjacency(rows, contexts),
        "adjacency_120": lambda rows: refine_adjacency(rows, contexts, 120),
        "adjacency_400": lambda rows: refine_adjacency(rows, contexts, 400),
    }
    wanted = args.constraint or list(strategies)

    base_of = lambda r: canonical(r.get("predicted"))            # noqa: E731
    base_c, n = score(ordered, base_of, groups)
    print(f"  rows scored        {n}")
    print(f"  baseline accuracy  {base_c}/{n} = {base_c/n:.1%}\n")
    print(f"  {'constraint':14s} {'changed':>8s} {'accuracy':>9s} "
          f"{'fixed':>6s} {'broke':>6s} {'McNemar':>11s}  verdict")

    results = {}
    for name in wanted:
        repaired, changed = strategies[name](ordered)
        ref_of = lambda r: repaired.get(r["id"], canonical(r.get("predicted")))  # noqa: E731
        ref_c, _ = score(ordered, ref_of, groups)
        fixed = [r for r in ordered
                 if not same_speaker(base_of(r), r.get("expected"), groups)
                 and same_speaker(ref_of(r), r.get("expected"), groups)]
        broke = [r for r in ordered
                 if same_speaker(base_of(r), r.get("expected"), groups)
                 and not same_speaker(ref_of(r), r.get("expected"), groups)]
        p, _b, _c = exact_mcnemar(len(fixed), len(broke))
        verdict = ("HELPS" if len(fixed) > len(broke) and p < 0.05
                   else "hurts" if len(broke) > len(fixed) and p < 0.05
                   else "no separation")
        results[name] = {
            "predictions_changed": changed,
            "refined_correct": ref_c, "refined_accuracy": round(ref_c / n, 4),
            "delta_points": round(100 * (ref_c - base_c) / n, 2),
            "fixed": len(fixed), "broke": len(broke), "mcnemar_p": p,
            "verdict": verdict,
            "fixed_examples": [r["id"] for r in fixed[:8]],
            "broke_examples": [r["id"] for r in broke[:8]],
        }
        print(f"  {name:14s} {changed:8d} {ref_c/n:8.1%} "
              f"{len(fixed):6d} {len(broke):6d} {p:11.2e}  {verdict}")

    payload = {
        "scope": "constraint repairs of the model's speaker assignment, each "
                 "applied ALONE and paired against the model's own output on "
                 "identical rows",
        "artifact": os.path.basename(args.artifact),
        "rows_scored": n,
        "baseline_correct": base_c, "baseline_accuracy": round(base_c / n, 4),
        "constraints": results,
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
