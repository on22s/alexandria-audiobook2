"""Would a scene-sized candidate list beat the whole cast?

Prompted by a character co-occurrence network (Xue & Lu, EITCE 2024). Their
execution is a word cloud, but the idea underneath - restrict the candidates to
the characters who are actually present - attacks the place our error measurably
lives: the roster holds the right name and the model does not pick it.

This measures the CEILING offline, so the question is answered before any GPU
time is spent on re-prompting. For each row it ranks the cast by how recently
each name appears in the text before the quote, keeps the nearest K, and asks
what narrowing to that list could do:

    gained   wrong now, and the window excludes the model's answer while
             still containing the gold - a shorter list could only help here
    lost     correct now, and the gold falls OUTSIDE the window - narrowing
             takes a right answer away
    local    wrong now, and BOTH the gold and the model's answer are inside
             the window - a shorter list changes nothing
    absent   wrong now, and the gold is outside the window anyway

`gained - lost` is the best case, reached only if the model always picks
correctly from the shorter list, which it will not. A negative best case is a
decisive answer and costs nothing to obtain.

PDNC only. The light-novel fixtures carry no context to search; recovering it
needs the source aligner on the branch for #373.
"""
import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, normalize, same_speaker  # noqa: E402

DEFAULT_K = (1, 2, 3, 5, 10, 20)


def surface_forms(name, groups):
    """-> every spelling that denotes `name`, including its alias group."""
    key = normalize(name)
    forms = {key}
    for group in groups:
        if key in group:
            forms |= set(group)
    return {f for f in forms if f}


def recent_mentions(text, roster, groups):
    """-> cast members ordered by LAST appearance in `text`, nearest the end first.

    Matching is word-bounded. Without \\b, MARIA matches inside Marianne and
    ANNE inside Anneliese, which silently inflates how close a character was to
    the quote - the measurement would then be of the regex, not of the prose.
    """
    text = text or ""
    position = {}
    for name in roster:
        for form in surface_forms(name, groups):
            pattern = re.compile(r"\b%s\b" % re.escape(form), re.IGNORECASE)
            for hit in pattern.finditer(text):
                position[name] = max(position.get(name, -1), hit.start())
    return [n for n, _ in sorted(position.items(), key=lambda kv: -kv[1])]


def load_gold(fixtures_dir):
    gold, rosters, groups = {}, {}, {}
    for path in sorted(glob.glob(os.path.join(
            fixtures_dir, "attribution_gold_pdnc_*.json"))):
        book = os.path.basename(path)[:-len(".json")]
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        rosters[book] = list(fixture.get("roster") or [])
        groups[book] = alias_groups(fixture)
        for entry in fixture.get("entries") or []:
            gold[(book, entry["id"])] = entry
    return gold, rosters, groups


def analyse(rows, gold, rosters, groups, window, ks):
    coverage = {k: 0 for k in ks}
    buckets = {"gained": 0, "lost": 0, "local": 0, "absent": 0, "safe": 0}
    scored, in_roster, examples = 0, 0, []
    for row in rows:
        book, _, quote_id = (row.get("id") or "").partition(":")
        entry = gold.get((book, quote_id))
        if entry is None:
            continue
        scored += 1
        roster, g = rosters.get(book, ()), groups.get(book, ())
        expected, predicted = row.get("expected"), row.get("predicted")
        if any(same_speaker(expected, n, g) for n in roster):
            in_roster += 1
        near = recent_mentions(entry.get("prev_context"), roster, g)
        for k in ks:
            if any(same_speaker(expected, n, g) for n in near[:k]):
                coverage[k] += 1
        keep = near[:window]
        gold_in = any(same_speaker(expected, n, g) for n in keep)
        pred_in = any(same_speaker(predicted, n, g) for n in keep)
        if row.get("correct"):
            buckets["lost" if not gold_in else "safe"] += 1
        elif gold_in and not pred_in:
            buckets["gained"] += 1
            if len(examples) < 8:
                examples.append({"id": row.get("id"), "expected": expected,
                                 "predicted": predicted})
        elif gold_in:
            buckets["local"] += 1
        else:
            buckets["absent"] += 1
    return coverage, buckets, scored, in_roster, examples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    parser.add_argument("--window", type=int, default=10,
                        help="how many recently-mentioned names to keep")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)
    gold, rosters, groups = load_gold(args.fixtures)
    coverage, buckets, scored, in_roster, examples = analyse(
        artifact.get("rows") or [], gold, rosters, groups, args.window, DEFAULT_K)
    if not scored:
        raise SystemExit("no rows matched the gold; check --fixtures")

    sizes = [len(rosters[b]) for b in rosters if rosters[b]]
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "ceiling of restricting candidates to the K most recently "
                 "mentioned cast members; no model was re-run",
        "source_artifact": os.path.basename(args.artifact),
        "rows_scored": scored,
        "window": args.window,
        "mean_roster_size": round(sum(sizes) / len(sizes), 1) if sizes else None,
        "gold_in_full_roster": round(in_roster / scored, 4),
        "coverage_by_k": {str(k): round(v / scored, 4) for k, v in coverage.items()},
        "buckets": buckets,
        "best_case_rows": buckets["gained"] - buckets["lost"],
        "best_case_points": round(
            100 * (buckets["gained"] - buckets["lost"]) / scored, 2),
        "gained_examples": examples,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("rows %d | mean roster %.0f | gold in full roster %.3f"
          % (scored, doc["mean_roster_size"], doc["gold_in_full_roster"]))
    print("coverage:", " ".join("K=%s %.3f" % (k, v)
                                for k, v in doc["coverage_by_k"].items()))
    print("window %d -> gained %d, lost %d, local %d, absent %d | best case %+d rows (%+.2f pts)"
          % (args.window, buckets["gained"], buckets["lost"], buckets["local"],
             buckets["absent"], doc["best_case_rows"], doc["best_case_points"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
