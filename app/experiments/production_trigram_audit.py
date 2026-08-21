"""What would the trigram pre-pass change in scripts we have already shipped?

#372 measured the rule against gold on PDNC: it fires on 4.0% of rows and is
right .9899 of the time there, fixing 31 and breaking 0. #373 measured it on
four light novels: 10 of 10 correct, but it fires on only 1.3% of rows because
those books rarely use the construction.

Neither says what it would do to OUR OUTPUT. This does. It runs the same
classifier over the 29 retrofitted library scripts - real generated books, no
gold - and reports where the rule and the pipeline disagree.

WITHOUT GOLD THIS MEASURES DISAGREEMENT, NOT ACCURACY, and the two must not be
conflated. What licenses reading a disagreement as a likely correction is the
external evidence, not this file: the same rule scores .99 on PDNC, 1.00 on the
light novels, and Elson & McKeown reported .99 for the same category in 2010.
A disagreement is a candidate, and the artifact keeps every one of them with
its surrounding text so they can be read.
"""
import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.elson_trigram import APPLY_BY_DEFAULT, classify  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, normalize, same_speaker  # noqa: E402

WINDOW = 200
UNATTRIBUTED = {"", "NARRATOR", "UNKNOWN", "?"}


def load_aliases(path):
    """character_aliases.json is {canonical: [alias, ...]} -> scoring groups."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []
    groups = []
    for name, aliases in (raw or {}).items():
        members = {normalize(name)} | {normalize(a) for a in (aliases or [])}
        members = {m for m in members if m}
        if len(members) > 1:
            groups.append(members)
    return groups


def span_of(entry):
    """source_span survives a JSON round trip as a list or as its repr."""
    span = entry.get("source_span")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        return int(span[0]), int(span[1])
    if isinstance(span, str):
        try:
            parsed = json.loads(span)
            return int(parsed[0]), int(parsed[1])
        except (ValueError, TypeError, IndexError):
            return None
    return None


def is_spoken(entry):
    value = entry.get("spoken")
    return value is True or value == "True"


def audit_script(entries, source, roster, groups):
    """-> (counts, disagreements) for one book."""
    counts = {"entries": len(entries), "spoken": 0, "with_span": 0,
              "fired": 0, "agree": 0, "disagree": 0,
              "fired_on_unattributed": 0}
    out = []
    for entry in entries:
        if not is_spoken(entry):
            continue
        counts["spoken"] += 1
        span = span_of(entry)
        if span is None:
            continue
        counts["with_span"] += 1
        start, end = span
        category, implied = classify(
            entry.get("text"), source[max(0, start - WINDOW):start],
            source[end:end + WINDOW], roster, groups)
        if not implied or category not in APPLY_BY_DEFAULT:
            continue
        counts["fired"] += 1
        assigned = (entry.get("speaker") or "").strip()
        if normalize(assigned) in UNATTRIBUTED:
            counts["fired_on_unattributed"] += 1
            counts["disagree"] += 1
        elif same_speaker(assigned, implied, groups):
            counts["agree"] += 1
            continue
        else:
            counts["disagree"] += 1
        out.append({"text": (entry.get("text") or "")[:120],
                    "assigned": assigned or None, "rule": implied,
                    "category": category,
                    "after": source[end:end + 80].replace("\n", " ")})
    return counts, out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts", default=os.path.join(
        REPO, "ab_test_runtime", "retrofit_library"))
    parser.add_argument("--sources", nargs="+", required=True)
    parser.add_argument("--aliases", default=os.path.join(
        REPO, "character_aliases.json"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    groups = load_aliases(args.aliases)
    sources = {}
    for root in args.sources:
        for path in glob.glob(os.path.join(root, "*.txt")):
            sources[os.path.basename(path)[:-4]] = path

    books, totals, missing = [], {}, []
    for path in sorted(glob.glob(os.path.join(args.scripts, "*.json"))):
        name = os.path.basename(path)[:-len(".json")]
        # "Arc 1 - Volume 1_3" is a re-run of "Arc 1 - Volume 1".
        base = name.rsplit("_", 1)[0] if name.rsplit("_", 1)[-1].isdigit() else name
        if base not in sources:
            missing.append(name)
            continue
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
        with open(sources[base], encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        roster = sorted({(e.get("speaker") or "").strip() for e in entries
                         if normalize(e.get("speaker")) not in UNATTRIBUTED})
        counts, rows = audit_script(entries, source, roster, groups)
        counts["book"] = name
        counts["roster"] = len(roster)
        books.append(counts)
        for key, value in counts.items():
            if isinstance(value, int) and key != "roster":
                totals[key] = totals.get(key, 0) + value
        for row in rows:
            row["book"] = name
        books[-1]["disagreements"] = rows

    fired = totals.get("fired", 0)
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "the #372 trigram run over shipped scripts; no gold, so this "
                 "reports DISAGREEMENT with the pipeline, not accuracy",
        "books": len(books),
        "sources_missing_for": missing,
        "totals": totals,
        "fire_rate_on_spoken": round(fired / totals["with_span"], 4)
        if totals.get("with_span") else None,
        "disagreement_rate_where_fired": round(
            totals.get("disagree", 0) / fired, 4) if fired else None,
        "per_book": books,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)
    print("books %d | spoken %d | with span %d | fired %d (%.2f%% of spanned)"
          % (len(books), totals.get("spoken", 0), totals.get("with_span", 0),
             fired, 100 * (doc["fire_rate_on_spoken"] or 0)))
    print("agree %d | disagree %d (%.1f%% of fired), of which %d were unattributed"
          % (totals.get("agree", 0), totals.get("disagree", 0),
             100 * (doc["disagreement_rate_where_fired"] or 0),
             totals.get("fired_on_unattributed", 0)))
    if missing:
        print("no source for %d scripts: %s" % (len(missing), ", ".join(missing[:4])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
