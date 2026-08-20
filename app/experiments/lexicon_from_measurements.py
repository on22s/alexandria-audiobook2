"""Turn the respelling measurements into the record goal 5.5 asks for.

THE TARGET, in its own words: "every term in the shipped books whose plain form
does not produce the word either has a measured entry or is recorded as one
respelling could not fix". Both halves, and the second is the larger.

WHAT MAKES THIS ANSWERABLE NOW. 7,775 terms have been measured and re-scored
against the shipped derivation, and 6,637 of them are terms the plain reading
fails to say - the population this goal is about. For each, the artifact already
records whether the respelling rescued it. Nothing needs generating.

WHY IT IS NOT SIMPLY "ADD EVERY RESPELLING". Measured on 1,582 terms across all
rows, respelling BREAKS 69.7% of the words the engine already said correctly
(respelling_selectivity.json). An entry is therefore only ever proposed for a
term the plain reading fails - never as a blanket substitution - which is also
what pronunciation.json's own header has always said: "add one only when a
candidate has been MEASURED to help".

THE THIRD STATE IS REAL AND IS NOT HIDDEN. A term can be measured-and-rescued,
measured-and-not-rescued, or not measured at all. Only the first two are what
the target asks for; the third is reported separately rather than being folded
into "could not fix", which would claim a measurement that was never taken.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

BASELINE = os.path.join(REPO, "ab_test_runtime", "experiments",
                        "respelling_measure_rescored.json")


def load(paths):
    """-> {term: [row, ...]} across every arm supplied."""
    by_term = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        for row in doc.get("results") or []:
            if "plain_recovers_word" not in row:
                continue
            by_term.setdefault(row["term"], []).append(
                dict(row, _arm=os.path.basename(path)))
    return by_term


def classify(by_term):
    """-> (entries, could_not_fix, plain_already_works).

    A term counts as rescued if ANY measured arm produced the word where the
    plain reading did not. The arm that did it is recorded with the entry, so a
    reader can see which derivation earned the claim rather than trusting the
    shipped one by default.
    """
    entries, unfixable, fine = {}, [], []
    for term, rows in sorted(by_term.items()):
        fails = [r for r in rows if not r["plain_recovers_word"]]
        if not fails:
            fine.append(term)
            continue
        rescued = [r for r in fails if r.get("respelled_recovers_word")]
        if rescued:
            # HOW MANY ARMS AGREED, recorded per entry. Taking "rescued by ANY
            # arm" is a selection effect on a pipeline whose noise floor is not
            # small - 34 of 391 verdicts flipped on IDENTICAL input in the
            # plain control - so a term rescued by one arm of three may have
            # been rescued by chance. A term rescued where only one arm
            # measured it is a single reading; one rescued by two of two is
            # corroborated. The caller decides which to ship; this records
            # which it is rather than presenting them as the same claim.
            best = rescued[0]
            entries[term] = {
                "respelling": best.get("respelling"),
                "kana": best.get("kana"),
                "arm": best["_arm"],
                "arms_rescuing": len(rescued),
                "arms_measuring": len(fails),
                "corroborated": len(rescued) > 1,
                "books": best.get("books"),
            }
        else:
            unfixable.append({
                "term": term, "kana": fails[0].get("kana"),
                "books": fails[0].get("books"),
                "arms_tried": sorted({r["_arm"] for r in fails}),
                "plain_heard": fails[0].get("plain_heard"),
                "respelled_heard": fails[0].get("respelled_heard"),
            })
    return entries, unfixable, fine


def corroboration_rate(entries):
    """-> how often a rescue reproduced, among terms measured more than once."""
    multi = [e for e in entries.values() if e["arms_measuring"] > 1]
    agreed = sum(1 for e in multi if e["corroborated"])
    # ONE SHAPE, ALWAYS. An early return with fewer keys makes every reader
    # write a .get() and one of them eventually forgets.
    return {
        "terms_measured_by_more_than_one_arm": len(multi),
        "rescued_by_more_than_one": agreed,
        "rescued_by_exactly_one": len(multi) - agreed,
        "agreement_pct": round(100.0 * agreed / len(multi), 1) if multi else None,
        "terms_measured_once_only": sum(
            1 for e in entries.values() if e["arms_measuring"] == 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arms", nargs="+", default=[BASELINE])
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_candidates.json"))
    parser.add_argument("--write-lexicon", default=None,
                        help="also write a pronunciation.json-shaped file of "
                             "the entries. Not written by default: shipping a "
                             "lexicon is a product decision, not a measurement")
    args = parser.parse_args()

    by_term = load(args.arms)
    entries, unfixable, fine = classify(by_term)
    total = len(by_term)
    covered = len(entries) + len(unfixable)
    payload = {
        "note": ("Goal 5.5's two halves, from measurement alone. A term the "
                 "plain reading fails either gets an entry that was measured "
                 "to rescue it, or is recorded as one respelling could not "
                 "fix. Terms the plain reading already says are excluded - "
                 "respelling breaks 69.7% of those."),
        "arms_read": [os.path.basename(a) for a in args.arms],
        "terms_measured": total,
        "plain_already_says_the_word": len(fine),
        "entries_measured_to_help": len(entries),
        "entries_corroborated_by_more_than_one_arm": sum(
            1 for e in entries.values() if e["corroborated"]),
        "entries_from_a_single_reading": sum(
            1 for e in entries.values() if not e["corroborated"]),
        # THE NUMBER THAT SAYS HOW MUCH TO TRUST THE ENTRIES. Restricted to
        # terms more than one arm actually measured, how often did more than
        # one arm rescue it? Measured 2026-08-20: 101 of 380, 26.6%. Some of
        # the disagreement is a real separator effect - the hyphen rescues
        # 15.2% against the no-separator form's 10.2% - and the rest is the
        # pipeline's own churn. Either way a lone rescue is weak evidence, and
        # a lexicon built from 1,056 of them would be the 38%-versus-13%
        # mistake again.
        "corroboration": corroboration_rate(entries),
        "recorded_as_unfixable": len(unfixable),
        "coverage_of_the_failing_band": round(100.0 * covered / total, 1) if total else None,
        "entries": entries,
        "could_not_fix": unfixable,
        "provenance": provenance(__file__, args),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print("measured terms          %d" % total)
    print("  plain already says it %d  (an entry here would do harm)" % len(fine))
    corroborated = sum(1 for e in entries.values() if e["corroborated"])
    print("  measured to help      %d  -> entries" % len(entries))
    print("      of those, corroborated by >1 arm  %d" % corroborated)
    print("      resting on a single reading       %d" % (len(entries) - corroborated))
    rate = corroboration_rate(entries)
    if rate.get("terms_measured_by_more_than_one_arm"):
        print("      among terms measured by >1 arm, a rescue reproduced "
              "%.1f%% of the time (%d of %d)"
              % (rate["agreement_pct"], rate["rescued_by_more_than_one"],
                 rate["terms_measured_by_more_than_one_arm"]))
    print("  could not fix         %d  -> recorded, which the target accepts"
          % len(unfixable))
    print("wrote %s" % args.out)

    if args.write_lexicon:
        lexicon = {"_comment": ("Generated by lexicon_from_measurements.py from "
                                "measured rescues only. Every entry rescued a "
                                "term the plain reading failed to say."),
                   "_generated": payload["provenance"]["written"]}
        lexicon.update({t: e["respelling"] for t, e in entries.items()
                        if e["respelling"]})
        with open(args.write_lexicon, "w", encoding="utf-8") as handle:
            json.dump(lexicon, handle, indent=1, ensure_ascii=False)
        print("wrote %s (%d entries)" % (args.write_lexicon, len(entries)))


if __name__ == "__main__":
    main()
