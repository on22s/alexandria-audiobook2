#!/usr/bin/env python3
"""Which light-novel results were measured on half the gold?

WHY THIS EXISTS. The four annotated Japanese light novels hold 793 gold rows,
and `grimgar03` is 396 of them - half the corpus by itself. It is missing from
54 of the 58 multi-book evaluations ever run: every three-book artifact omits
the same book, and only three artifacts (all 2026-08-23) cover all four.

So the per-book tables, the adapter rankings, and the "Qwen3.8 adapters are a
null" verdict were computed on the other three books. That is not a rounding
difference. grimgar03's base arm reads 89.1% against 68-75% for the others, so
adding it moves any pooled figure upward for reasons unrelated to method, and
a table mixing three-book and four-book artifacts is comparing two corpora.

WHAT THIS SCRIPT DOES. It reports coverage per artifact and per book, and names
the artifacts that current conclusions rest on so they can be re-run at four
books. It computes nothing about accuracy: a coverage audit that also reported
scores would invite exactly the mixed-corpus comparison it exists to prevent.

DELIBERATELY NOT A REFUSAL. A three-book artifact is not invalid - it measured
what it measured. The defect is reading it as a light-novel result, which is a
claim about the corpus rather than about the file.
"""
import argparse
import collections
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOOKS = ("grimgar03", "index18", "mushoku16", "owarimonogatari3")


def coverage(experiment_dir):
    """-> [(artifact, sorted books, rows)] for every artifact touching a light novel."""
    out = []
    for path in sorted(glob.glob(os.path.join(experiment_dir, "distill_eval__*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        rows = doc.get("rows") or []
        if not rows:
            continue
        seen = {str(r.get("id", "")).split(":")[0] for r in rows} & set(BOOKS)
        if seen:
            out.append((os.path.basename(path), sorted(seen), len(rows)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--experiments", default=os.path.join(
        REPO, "ab_test_runtime", "experiments"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "light_novel_coverage.json"))
    args = ap.parse_args()

    rows = coverage(args.experiments)
    if not rows:
        sys.exit(f"no light-novel artifacts under {args.experiments}; nothing "
                 f"was audited. Point --experiments at a checkout that has them.")

    per_book = collections.Counter()
    missing = collections.Counter()
    for _, books, _ in rows:
        for b in books:
            per_book[b] += 1
        for b in set(BOOKS) - set(books):
            if len(books) > 1:
                missing[b] += 1
    full = [r for r in rows if len(r[1]) == 4]

    doc = {
        "note": "Coverage only. No accuracy is reported here on purpose: a "
                "table mixing three-book and four-book artifacts compares two "
                "corpora, and that is the error this audit exists to surface.",
        "gold_rows": {"grimgar03": 396, "owarimonogatari3": 162,
                      "mushoku16": 136, "index18": 99},
        "artifacts_touching_a_light_novel": len(rows),
        "artifacts_covering_all_four": len(full),
        "appearances_per_book": dict(per_book.most_common()),
        "absent_from_multi_book_artifacts": dict(missing.most_common()),
        "full_coverage_artifacts": [r[0] for r in full],
        "limitations": [
            "A three-book artifact is not invalid; it measured what it "
            "measured. The defect is reading it as a light-novel result.",
            "grimgar03 is 396 of 793 gold rows, and its base arm reads 89.1% "
            "against 68-75% for the others, so adding it raises any pooled "
            "figure for reasons unrelated to method.",
        ],
    }
    try:
        sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
        from provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print(f"artifacts touching a light novel : {len(rows)}")
    print(f"  covering all four              : {len(full)}")
    for book, n in per_book.most_common():
        print(f"    {book:20} appears in {n}")
    for book, n in missing.most_common():
        print(f"    {book:20} ABSENT from {n} multi-book artifacts")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
