#!/usr/bin/env python3
"""Which of goal 5.5's measured terms actually occur in the books we ship?

WHY THIS EXISTS. `lexicon_candidates.json` records, per term, how many books
contain it - a COUNT, not a list, and counted over the whole light-novel
library rather than over the 29 saved scripts. So the record says "every term
the plain reading fails is in one of two states" about the MEASURED CORPUS,
while 5.5's target is about THE SHIPPED BOOKS. Those are different populations
and nothing had mapped one onto the other. GOALS calls this scan cheap and
unrun; this is it.

WHAT IT ANSWERS. It DISCOVERS the foreign terms in the shipped books using
`discover_foreign_terms.build_detector` and `roster_forms` - the same test that
built the candidate list, imported rather than reimplemented (Rule 15) - and
then asks of each one: is it
  - an ENTRY      (measured to help), or
  - UNFIXABLE     (recorded as one respelling could not fix), or
  - PLAIN-OK      (the engine already says it; an entry would do HARM), or
  - NEITHER       <- the only category that keeps 5.5 open.

THE THIRD STATE WAS MISSING UNTIL 2026-09-04, and its absence made this scan
report correct work as a gap. `plain_already_works` is the state 5.5 arrived at
by measurement - respelling breaks 69.7% of the words the engine already says,
so those terms must NOT get an entry - but `lexicon_candidates.json` stored
only a count of them, so this scan could not recognise one and filed it under
NEITHER. Nine of the fifteen terms it last reported as uncovered were in fact
in this state, including `manga`, which appears in 3,602 books and is said
correctly. A check that reports a finished term as unfinished is the failure
goal 6.6 is about: it cannot be satisfied, so it carries no information.

DO NOT PARSE THE DISCOVERY SCRIPT'S STDOUT. A first version of this did, with
awk, and silently produced three terms that were words from the report's own
prose - `candidate`, `only;`, `scripts,`. The detector is a function; call it.

WHAT IT DELIBERATELY DOES NOT DO. It stores no sentences. Output is a per-book
count and a list of terms, which is vocabulary statistics, not an extract of
anyone's book - the same line `lexicon_corpus_scan.py` draws.

A NOTE ON MATCHING. Terms are matched as whole words, case-folded. Substring
matching would count `same` inside `sameness` and inflate every number; the
corpus scan learned that the hard way with contraction tails and name
fragments.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORD = re.compile(r"[a-z0-9']+")


def load_states(path):
    """-> (entries, unfixable, plain_ok) as three sets of case-folded terms.

    A candidates file written before `plain_already_works` existed has only a
    count, and there is no way to recover the list from it. Rather than guess,
    the third set comes back empty and `main` says so - an old artifact then
    reports the coverage it always did, instead of a number that silently
    depends on which version wrote its input.
    """
    doc = json.load(open(path, encoding="utf-8"))
    entries = {t.lower() for t in doc.get("entries", {})}
    unfixable = {r["term"].lower() for r in doc.get("could_not_fix", [])
                 if isinstance(r, dict) and r.get("term")}
    plain_ok = {t.lower() for t in doc.get("plain_already_works", [])}
    return entries, unfixable, plain_ok


def shipped_scripts(scripts_dir):
    """The saved books, excluding the sidecars that sit beside them."""
    out = []
    for path in sorted(glob.glob(os.path.join(scripts_dir, "*.json"))):
        name = os.path.basename(path)
        if ".generation_" in name or "voice_config" in name:
            continue
        out.append(path)
    return out


def terms_in_script(path):
    """-> the set of whole words used in one script's spoken text."""
    doc = json.load(open(path, encoding="utf-8"))
    rows = doc if isinstance(doc, list) else (doc.get("entries") or [])
    seen = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("text"), str):
            seen.update(WORD.findall(row["text"].lower()))
    return seen


def discover(scripts):
    """-> the foreign terms the shipped books actually contain.

    Imports the detector rather than shelling out, so this cannot drift from
    the test that produced the candidate list.
    """
    sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
    from discover_foreign_terms import build_detector, roster_forms
    is_foreign, why = build_detector()
    if is_foreign is None:
        raise SystemExit("detector unavailable: %s" % why)
    names = roster_forms(scripts)
    found = collections.defaultdict(set)
    for path in scripts:
        book = os.path.basename(path)[:-5]
        for word in terms_in_script(path):
            if word in names:
                continue
            if is_foreign(word):
                found[word].add(book)
    return found


def scan(scripts_dir, candidates):
    entries, unfixable, plain_ok = load_states(candidates)
    scripts = shipped_scripts(scripts_dir)
    term_books = discover(scripts)
    per_book = collections.Counter()
    for term, books in term_books.items():
        for b in books:
            per_book[b] += 1
    return entries, unfixable, plain_ok, scripts, per_book, term_books


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", default=os.path.join(REPO, "scripts"))
    ap.add_argument("--candidates", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_candidates.json"))
    ap.add_argument("--triage", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "shipped_term_triage.json"),
        help="terms judged out of scope for a loanword lexicon; reported "
             "beside the coverage figure and deliberately NOT counted in it")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "shipped_book_lexicon_coverage.json"))
    args = ap.parse_args()

    entries, unfixable, plain_ok, scripts, per_book, term_books = scan(
        args.scripts, args.candidates)
    books = len(scripts)
    # NO BOOKS IS NOT 100% COVERAGE, AND IT IS NOT 0% EITHER. Run from a
    # worktree, where `scripts/` is not checked out, this printed "0 books,
    # coverage None" and exited 0 - a scan that measured nothing and said so
    # only in a field nobody reads. Same family as the third-state bug above:
    # refuse rather than report.
    if not scripts:
        sys.exit(f"no saved scripts under {args.scripts}; nothing was scanned. "
                 f"Point --scripts at a checkout that has them.")
    present = sorted(term_books)
    handled = entries | unfixable | plain_ok
    neither = [t for t in present if t not in handled]
    # TRIAGE IS A JUDGEMENT AND IS KEPT OUT OF THE COVERAGE FIGURE. Knowing
    # that `gaurururu` is a growl and `masaharu` a person is useful - it stops
    # the same eighteen terms being re-triaged - but it was decided by reading
    # sentences and looking words up, not by measuring anything. Folding it
    # into coverage_percent would let a judgement close a measurement goal,
    # which is exactly the move Rule 19 exists to prevent. It is reported
    # alongside, and the reader can see which is which.
    triaged = {}
    if os.path.exists(args.triage):
        tdoc = json.load(open(args.triage, encoding="utf-8"))
        for kind, rows in (tdoc.get("classified") or {}).items():
            for row in rows:
                term = (row.get("term") or "").lower()
                if term:
                    triaged[term] = kind
    doc = {
        "note": "Maps goal 5.5's measured terms onto the SHIPPED books. The "
                "candidate record counts books over the whole library; this "
                "names which saved scripts each term actually occurs in.",
        "shipped_books": books,
        "terms_measured_total": len(entries) + len(unfixable) + len(plain_ok),
        "terms_present_in_shipped_books": len(present),
        "present_with_entry": sum(1 for t in present if t in entries),
        "present_recorded_unfixable": sum(1 for t in present if t in unfixable),
        "present_plain_already_works": sum(1 for t in present if t in plain_ok),
        "third_state_available": bool(plain_ok),
        "present_in_neither_state": len(neither),
        "coverage_percent": round(
            100.0 * (len(present) - len(neither)) / len(present), 1) if present else None,
        "terms_in_neither_state": sorted(neither),
        "neither_state_with_a_triage_judgement": {
            t: triaged[t] for t in sorted(neither) if t in triaged},
        "neither_state_unexplained": sorted(
            t for t in neither if t not in triaged),
        "per_book": [{"book": b, "foreign_terms": n} for b, n in sorted(per_book.items())],
        "term_to_books": {t: sorted(term_books[t]) for t in present},
    }
    try:
        sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
        from provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    print(f"shipped books scanned      : {books}")
    print(f"measured terms in total    : {doc['terms_measured_total']}")
    print(f"  present in shipped books : {doc['terms_present_in_shipped_books']}")
    print(f"    with a measured entry  : {doc['present_with_entry']}")
    print(f"    recorded as unfixable  : {doc['present_recorded_unfixable']}")
    print(f"    plain already says it  : {doc['present_plain_already_works']}"
          f"   (an entry here would do harm)")
    print(f"    NEITHER state          : {doc['present_in_neither_state']}"
          f"   (coverage {doc['coverage_percent']}%)")
    if neither:
        judged = doc["neither_state_with_a_triage_judgement"]
        print(f"\n  of the {len(neither)} in neither state, "
              f"{len(judged)} carry a triage judgement (NOT counted as "
              f"coverage - a judgement, not a measurement):")
        for term, kind in judged.items():
            print(f"      {term:14} {kind}")
        rest = doc["neither_state_unexplained"]
        if rest:
            print(f"      unexplained: {', '.join(rest)}")
    if not plain_ok:
        print("\n  NOTE: this candidates file predates `plain_already_works` "
              "and carries only a count, so terms the plain reading already "
              "says cannot be recognised and are counted as uncovered. "
              "Regenerate it with lexicon_from_measurements.py.")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
