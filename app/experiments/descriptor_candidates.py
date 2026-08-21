"""Are "A HINDOO SERVANT" and "MR. DARCY" the same kind of candidate?

Our own cast lists are roughly a third ROLE LABELS rather than names -
`CROWD MEMBER 3`, `MAN WITH KNIFE`, `MAID 2 (EMILIA'S)`, `LITTLE GIRL`. PDNC
rosters carry them too: `A BLUFF, GENIAL INSPECTOR`, `A HINDOO SERVANT`,
`A MAID`, `THE COLONEL`, `BUTLER`.

Two questions, and the second is the one that matters.

Does the model over-reach for them? A descriptor is generic and reusable, so it
is a plausible thing to fall back on when the text names nobody - which #386
argued is exactly what the cast list is for.

AND DO THEY CONFOUND #383? That measured a list-order bias: when the model is
wrong, its answer sits earlier in the alphabetical roster than the correct one
67.2% of the time. Descriptors begin with "A" and therefore sort to the TOP.
If the model over-picked descriptors, part of that bias would be a preference
for a KIND of candidate wearing the costume of a preference for POSITION.

It does not. Measured on the 2,494 stored rows:

    gold is a descriptor      35 rows   1.4%   accuracy .714
    gold is a named character 2459 rows 98.6%  accuracy .656
    wrong rows reaching for a descriptor when the gold was named: 18 of 838

Too few to move a 67.2% effect, so #383 stands as a positional finding.

WHAT THIS DOES NOT SETTLE. PDNC is 1.4% descriptors; our own Re:Zero cast is
about a third. The phenomenon that prompted the question is real in OUR books
and cannot be tested on the annotated corpus, because the annotated corpus
barely has it.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.two_stage_attribution import roster_lines  # noqa: E402

# An article, an embedded description, or a bare occupational noun.
DESCRIPTOR = re.compile(
    r"^(?:A|AN|THE)\s|,|^(?:BUTLER|MAID|SERVANT|COOK|BOY|GIRL|MAN|WOMAN|"
    r"CROWD|NARRATOR|GUIDE|CLOWN|FAN|CAT)$")


def is_descriptor(name):
    """-> True when a roster entry describes a role rather than naming a person."""
    return bool(DESCRIPTOR.search((name or "").upper().strip()))


def index_of(name, names, groups):
    for i, candidate in enumerate(names):
        if same_speaker(candidate, name, groups):
            return i
    return None


def analyse(rows, fixtures):
    gold, cast = {}, {}
    for stem, fixture in fixtures.items():
        cast[stem] = ([l.split(" [also")[0] for l in roster_lines(fixture)],
                      alias_groups(fixture))
        for entry in fixture.get("entries") or []:
            gold[(stem, entry["id"])] = entry

    by_kind = collections.defaultdict(lambda: {"n": 0, "correct": 0})
    reached = collections.Counter()
    roster_share = []
    for stem, (names, _) in cast.items():
        if names:
            roster_share.append(sum(is_descriptor(n) for n in names) / len(names))

    for row in rows:
        stem, _, quote = (row.get("id") or "").partition(":")
        if (stem, quote) not in gold or stem not in cast:
            continue
        names, groups = cast[stem]
        gi = index_of(row.get("expected"), names, groups)
        if gi is None:
            continue
        kind = "descriptor" if is_descriptor(names[gi]) else "named"
        cell = by_kind[kind]
        cell["n"] += 1
        cell["correct"] += 1 if row.get("correct") else 0
        if not row.get("correct"):
            pi = index_of(row.get("predicted"), names, groups)
            if pi is not None:
                reached[(kind, "descriptor" if is_descriptor(names[pi])
                         else "named")] += 1

    total = sum(c["n"] for c in by_kind.values())
    return {
        "rows": total,
        "by_gold_kind": {k: {"n": v["n"],
                             "share": round(v["n"] / total, 4) if total else None,
                             "accuracy": round(v["correct"] / v["n"], 4)
                             if v["n"] else None}
                         for k, v in by_kind.items()},
        "wrong_rows_by_kind": {"%s -> %s" % k: v for k, v in reached.items()},
        "mean_roster_share_descriptors": round(
            sum(roster_share) / len(roster_share), 4) if roster_share else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    fixtures = {}
    for path in sorted(glob.glob(os.path.join(
            args.fixtures, "attribution_gold_pdnc_*_w3200.json"))):
        with open(path, encoding="utf-8") as handle:
            fixtures[os.path.basename(path)[:-len(".json")]] = json.load(handle)
    if not fixtures:
        raise SystemExit("no _w3200 fixtures under %s" % args.fixtures)
    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    result = analyse(artifact.get("rows") or [], fixtures)
    wrong_to_descriptor = result["wrong_rows_by_kind"].get("named -> descriptor", 0)
    wrong_total = sum(result["wrong_rows_by_kind"].values())
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "whether role-label candidates behave differently from named "
                 "ones, and whether they confound the ordering result in #383",
        **result,
        "confounds_383": wrong_to_descriptor / wrong_total > 0.25
        if wrong_total else None,
        "verdict": ("descriptors are too rare here to explain #383: they are "
                    "%.1f%% of gold answers and %d of %d wrong rows reach for "
                    "one. The ordering finding stands as positional. PDNC is "
                    "not the corpus to settle this on - our own cast lists are "
                    "roughly a third role labels and PDNC is under two percent"
                    % (100 * (result["by_gold_kind"].get("descriptor", {}).get("share") or 0),
                       wrong_to_descriptor, wrong_total)),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%d rows | rosters are %.1f%% descriptors on average\n"
          % (result["rows"], 100 * (result["mean_roster_share_descriptors"] or 0)))
    for kind, cell in sorted(result["by_gold_kind"].items()):
        print("  gold is a %-11s n=%-5d (%4.1f%%)  accuracy %.3f"
              % (kind, cell["n"], 100 * cell["share"], cell["accuracy"]))
    print("\n  wrong rows:")
    for k, v in sorted(result["wrong_rows_by_kind"].items()):
        print("    %-26s %d" % (k, v))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
