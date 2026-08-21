"""Does the model prefer names near the top of the cast list?

Three papers arrived on the same day saying LLMs favour early or prominent
items among CANDIDATES - Ermakova et al. on position bias in LLM responses,
Glater & Santos on answer-position bias in extractive QA, and the SIGIR '26
reproduction study, which notably FAILED to reproduce lost-in-the-middle for
evidence placement on models our size. Evidence position looks dead; candidate
position does not.

Our prompt lists the cast alphabetically - `roster_lines` ends in
`sorted(set(cast))` - so if the bias is real it is ours to remove, not the
model's to overcome.

THE UNPAIRED COMPARISON IS NOT ENOUGH and is reported only for context.
Predictions do sit earlier than gold on average, but alphabetical order is not
random with respect to who speaks: honorifics sort together, and a book's
busiest characters may cluster anywhere. The measurement that carries the
finding is PAIRED - for each WRONG row, is the model's answer earlier in that
same roster than the correct one? Cast composition cancels, and a coin would
say 50%.
"""
import argparse
import glob
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.scene_narrowing import recent_mentions  # noqa: E402
from experiments.two_stage_attribution import roster_lines  # noqa: E402


def position(name, names, groups):
    """-> where `name` sits in the roster, 0.0 first and 1.0 last, or None."""
    for index, candidate in enumerate(names):
        if same_speaker(candidate, name, groups):
            return index / max(1, len(names) - 1)
    return None


def sign_test(a, b):
    """Two-sided exact binomial p for a paired win/loss count."""
    n = a + b
    if not n:
        return 1.0
    k = min(a, b)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * tail / (2.0 ** n))


def analyse(rows, fixtures):
    gold, cast = {}, {}
    for stem, fixture in fixtures.items():
        cast[stem] = ([line.split(" [also")[0] for line in roster_lines(fixture)],
                      alias_groups(fixture))
        for entry in fixture.get("entries") or []:
            gold[(stem, entry["id"])] = entry

    all_pred, all_gold = [], []
    earlier = later = tied = 0
    shifts = []
    # Pezeshkpour & Hruschka (NAACL Findings 2024) report that option-order
    # sensitivity arises when a model is UNCERTAIN between its top choices. We
    # cannot read confidence - the artifact stores no logprobs - so the proxy
    # is contextual: a wrong row is a CLOSE CALL when both the gold speaker and
    # the model's answer are among the ten most recently mentioned characters,
    # the bucket #374 measured at 498 rows. It is a proxy for proximity, not
    # for confidence, and the two can disagree.
    strata = {"close_call": [0, 0], "no_local_signal": [0, 0]}
    for row in rows:
        stem, _, quote = (row.get("id") or "").partition(":")
        if (stem, quote) not in gold or stem not in cast:
            continue
        names, groups = cast[stem]
        p = position(row.get("predicted"), names, groups)
        g = position(row.get("expected"), names, groups)
        if p is None or g is None:
            continue
        all_pred.append(p)
        all_gold.append(g)
        if row.get("correct"):
            continue
        shifts.append(p - g)
        near = recent_mentions(gold[(stem, quote)].get("prev_context"),
                               names, groups)[:10]
        close = (any(same_speaker(row.get("expected"), n, groups) for n in near)
                 and any(same_speaker(row.get("predicted"), n, groups) for n in near))
        bucket = strata["close_call" if close else "no_local_signal"]
        if p < g:
            earlier += 1
            bucket[0] += 1
        elif p > g:
            later += 1
            bucket[1] += 1
        else:
            tied += 1

    n = len(all_pred)
    paired = earlier + later
    return {
        "rows_positioned": n,
        "unpaired": {
            "mean_gold_position": round(sum(all_gold) / n, 4) if n else None,
            "mean_predicted_position": round(sum(all_pred) / n, 4) if n else None,
            "note": "context only; alphabetical order is not random with "
                    "respect to who speaks",
        },
        "stratified_by_local_signal": {
            name: {"n": a + b, "share_earlier": round(a / (a + b), 4) if a + b else None,
                   "sign_test_p": sign_test(a, b)}
            for name, (a, b) in strata.items()},
        "paired_on_wrong_rows": {
            "wrong_answer_earlier_than_gold": earlier,
            "wrong_answer_later_than_gold": later,
            "same_position": tied,
            "share_earlier": round(earlier / paired, 4) if paired else None,
            "mean_signed_shift": round(sum(shifts) / len(shifts), 4) if shifts else None,
            "sign_test_p": sign_test(earlier, later),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    fixtures = {}
    for path in sorted(glob.glob(os.path.join(
            args.fixtures, "attribution_gold_pdnc_*.json"))):
        with open(path, encoding="utf-8") as handle:
            fixtures[os.path.basename(path)[:-len(".json")]] = json.load(handle)
    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    result = analyse(artifact.get("rows") or [], fixtures)
    if not result["rows_positioned"]:
        raise SystemExit("no row could be located in a roster")

    paired = result["paired_on_wrong_rows"]
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "where the model's answer sits in the alphabetical cast list "
                 "relative to the correct one, on rows it got wrong",
        "source_artifact": os.path.basename(args.artifact),
        "roster_order": "alphabetical, as roster_lines produces it",
        "verdict": ("the cast list's ORDER is influencing the answer; it is "
                    "alphabetical for no reason and can be changed"
                    if paired["sign_test_p"] < 0.01
                    and (paired["share_earlier"] or 0) > 0.55
                    else "no list-order effect at this sample size"),
        **result,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("rows positioned: %d" % result["rows_positioned"])
    print("unpaired  gold %.3f  predicted %.3f"
          % (result["unpaired"]["mean_gold_position"],
             result["unpaired"]["mean_predicted_position"]))
    print("paired, wrong rows only:")
    print("  earlier than gold %d (%.1f%%)   later %d"
          % (paired["wrong_answer_earlier_than_gold"],
             100 * paired["share_earlier"], paired["wrong_answer_later_than_gold"]))
    print("  mean signed shift %+.4f | sign test p = %.3g"
          % (paired["mean_signed_shift"], paired["sign_test_p"]))
    print("stratified by whether the context offered a local signal:")
    for name, cell in doc["stratified_by_local_signal"].items():
        print("  %-18s n=%-4d earlier %5.1f%%  p=%.2g"
              % (name, cell["n"], 100 * (cell["share_earlier"] or 0),
                 cell["sign_test_p"]))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
