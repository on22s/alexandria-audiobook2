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
  - NEITHER       <- the only category that keeps 5.5 open.

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
    """-> (entries, unfixable) as two sets of case-folded terms."""
    doc = json.load(open(path, encoding="utf-8"))
    entries = {t.lower() for t in doc.get("entries", {})}
    unfixable = {r["term"].lower() for r in doc.get("could_not_fix", [])
                 if isinstance(r, dict) and r.get("term")}
    return entries, unfixable


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
    entries, unfixable = load_states(candidates)
    scripts = shipped_scripts(scripts_dir)
    term_books = discover(scripts)
    per_book = collections.Counter()
    for term, books in term_books.items():
        for b in books:
            per_book[b] += 1
    return entries, unfixable, scripts, per_book, term_books


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", default=os.path.join(REPO, "scripts"))
    ap.add_argument("--candidates", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_candidates.json"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "shipped_book_lexicon_coverage.json"))
    args = ap.parse_args()

    entries, unfixable, scripts, per_book, term_books = scan(
        args.scripts, args.candidates)
    books = len(scripts)
    present = sorted(term_books)
    neither = [t for t in present if t not in entries and t not in unfixable]
    doc = {
        "note": "Maps goal 5.5's measured terms onto the SHIPPED books. The "
                "candidate record counts books over the whole library; this "
                "names which saved scripts each term actually occurs in.",
        "shipped_books": books,
        "terms_measured_total": len(entries) + len(unfixable),
        "terms_present_in_shipped_books": len(present),
        "present_with_entry": sum(1 for t in present if t in entries),
        "present_recorded_unfixable": sum(1 for t in present if t in unfixable),
        "present_in_neither_state": len(neither),
        "coverage_percent": round(
            100.0 * (len(present) - len(neither)) / len(present), 1) if present else None,
        "terms_in_neither_state": sorted(neither),
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
    print(f"    NEITHER state          : {doc['present_in_neither_state']}"
          f"   (coverage {doc['coverage_percent']}%)")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
