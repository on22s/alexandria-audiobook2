#!/usr/bin/env python3
"""Report whether attribution artifacts can support roster-conditioned claims.

An ``available`` summary of zero is ambiguous when every row carries
``in_candidates=null``: it means the evaluator did not record its roster, not
that the expected speaker was absent.  This audit separates those states and
groups them by experiment and producing commit.

IT IS ALSO NOT ENOUGH TO ASK WHETHER THE FIELD WAS RECORDED. A recorded
``in_candidates`` can be wrong, and 48 committed artifacts carry one that is.
``ExperimentRecord.add`` tests membership by exact match and asks callers to
pass the names each roster line stands for; two evaluators passed the display
roster instead, so a character shown as IZUKO GAEN did not satisfy a gold
asking for GAEN. Measured over the 57 re-checkable artifacts, 2,797 of 22,926
rows - 12.2% - are recorded as unavailable when the model was in fact offered
the character.

That silently biases a REPORTED metric. ``conditional`` is accuracy given the
answer was available, and the wrongly-excluded rows are ones the model gets
wrong more often than average, so excluding them flattered it: across those 48
artifacts the conditional figure moves by a median of -1.72 points and by as
much as -6.72. Raw accuracy is untouched - ``same_speaker`` was already
alias-aware - which is exactly why this went unnoticed.

So the recheck lives here, beside the recording-state audit, rather than in
whatever scratch script next asks the question.
"""
import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.scoring import (alias_groups, normalize,  # noqa: E402
                                 roster_membership_names)

FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def classify_rows(rows):
    """Return the roster-membership instrumentation state for artifact rows."""
    if not rows:
        return "no_rows"
    present = ["in_candidates" in row for row in rows]
    values = [row.get("in_candidates") for row in rows]
    if not any(present):
        return "field_absent"
    if all(value is None for value in values):
        return "all_null"
    if all(value is not None for value in values):
        return "populated"
    return "mixed"



def _book_of(row, gold_books):
    """Which gold a row belongs to. Ids are "book:book-00123" in multi-book
    runs and bare in single-book ones, where the meta names the only book."""
    row_id = str(row.get("id") or "")
    if ":" in row_id:
        return row_id.split(":", 1)[0]
    return gold_books[0] if len(gold_books) == 1 else None


def _alias_groups_for(book, fixtures, cache):
    if book not in cache:
        path = os.path.join(fixtures, "attribution_gold_%s.json" % book)
        try:
            with open(path, encoding="utf-8") as handle:
                cache[book] = alias_groups(json.load(handle))
        except (OSError, ValueError):
            cache[book] = None            # no gold here: not checkable
    return cache[book]


def recheck_membership(doc, fixtures=FIXTURES, cache=None):
    """-> what `in_candidates` SHOULD say, or None when it cannot be checked.

    Needs three things: rows that stored their candidate list, a gold fixture
    for the book, and alias groups in it. Missing any of them makes the answer
    unknown, and unknown is reported as None rather than as agreement - the
    same rule the field itself got wrong.
    """
    cache = {} if cache is None else cache
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    gold_books = list((meta.get("gold_files") or {}).keys())
    checked = moved = 0
    old_available = old_correct = new_available = new_correct = 0
    for row in doc.get("rows") or []:
        if not isinstance(row, dict) or not isinstance(row.get("candidates"), list):
            continue
        if not row["candidates"]:
            continue
        book = _book_of(row, gold_books)
        groups = _alias_groups_for(book, fixtures, cache) if book else None
        if groups is None:
            continue
        checked += 1
        available = normalize(row.get("expected")) in set(
            roster_membership_names(row["candidates"], groups))
        correct = bool(row.get("correct"))
        if row.get("in_candidates"):
            old_available += 1
            old_correct += correct
        if available:
            new_available += 1
            new_correct += correct
            if not row.get("in_candidates"):
                moved += 1
    if not checked:
        return None
    def ratio(c, a):
        return None if not a else c / a * 100
    return {
        "rows_checked": checked,
        "rows_wrongly_unavailable": moved,
        "available_recorded": old_available,
        "available_alias_aware": new_available,
        "conditional_recorded": ratio(old_correct, old_available),
        "conditional_alias_aware": ratio(new_correct, new_available),
    }

def audit_paths(paths, fixtures=FIXTURES):
    groups = collections.Counter()
    artifacts = []
    alias_cache = {}
    for path in sorted(paths):
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
            continue
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        experiment = meta.get("experiment") or doc.get("experiment") or "unknown"
        state = classify_rows(doc["rows"])
        commit = git.get("commit")
        groups[(experiment, commit, state)] += 1
        entry = {
            "artifact": os.path.basename(path),
            "experiment": experiment,
            "commit": commit,
            "rows": len(doc["rows"]),
            "candidate_membership": state,
        }
        recheck = recheck_membership(doc, fixtures, alias_cache)
        # Absent, not null: an artifact that cannot be re-checked and one whose
        # membership is correct must not read the same.
        if recheck is not None:
            entry["membership_recheck"] = recheck
        artifacts.append(entry)
    rechecked = [a["membership_recheck"] for a in artifacts
                 if "membership_recheck" in a]
    affected = [r for r in rechecked if r["rows_wrongly_unavailable"]]
    return {
        # The headline, so a reader does not have to fold 168 entries by hand
        # to notice that a third of them carry a wrong availability figure.
        "membership_recheck_summary": {
            "artifacts_rechecked": len(rechecked),
            "artifacts_not_checkable": len(artifacts) - len(rechecked),
            "artifacts_understating_availability": len(affected),
            "rows_checked": sum(r["rows_checked"] for r in rechecked),
            "rows_wrongly_unavailable": sum(
                r["rows_wrongly_unavailable"] for r in rechecked),
        },
        "groups": [
            {"experiment": experiment, "commit": commit,
             "candidate_membership": state, "artifacts": count}
            for (experiment, commit, state), count in sorted(
                groups.items(), key=lambda item: tuple(str(x) for x in item[0]))
        ],
        "artifacts": artifacts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="+", help="artifact files or globs")
    parser.add_argument("--out")
    args = parser.parse_args()
    paths = []
    for pattern in args.paths:
        matches = glob.glob(pattern, recursive=True)
        paths.extend(matches or [pattern])
    result = audit_paths(paths)
    if not result["artifacts"]:
        raise SystemExit("no readable row artifacts found; nothing was audited")
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
