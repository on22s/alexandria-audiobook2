"""Does the text we pull out of an EPUB match the book? (goal 7.2)

WHY THIS EXISTS. Extraction is the first stage of the pipeline and everything
downstream inherits its mistakes, yet it was the only stage measured solely
against fixtures we wrote ourselves. A fixture matches itself exactly, so it
cannot express the failure that matters.

WHAT THAT COST. On 2026-08-16 the new chapter-title recovery was measured by
hand against six real EPUBs for the first time: 89 of 89 TOC anchors resolved,
and all four titles it judged missing were ALREADY IN THE TEXT, differing only
by curly quotes, dash variants, or a `Volume 40` / `Light Novel` prefix. Zero
titles recovered, four headings duplicated - each one read aloud twice by the
narrator - with every unit test green throughout.

WHAT IS COUNTED, per book:

    resolved      TOC entries whose anchor was found in the spine document
    unresolved    entries pointing at an anchor no document contains
    inserted      titles judged missing and added
    duplicated    inserted titles that were already present nearby, which is
                  the defect above: a heading the listener hears twice

A duplicate is judged the way the extractor judges it, by calling the same
function. Reimplementing the comparison here would make this a test of a
second opinion rather than of the shipped behaviour.

NOT A METRIC OF PROSE QUALITY. It says nothing about whether the extracted
text reads well, only whether the structure survived.
"""
import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)


def measure(path):
    """-> per-book counts, by instrumenting the extractor rather than copying it."""
    from routers import script as script_module

    seen = {"resolved": 0, "unresolved": 0, "inserted": 0, "duplicated": 0,
            "insertions": []}
    original = script_module._insert_epub_toc_titles

    def spy(text, anchor_positions, toc_targets):
        for fragment, label in toc_targets:
            position = 0 if fragment is None else anchor_positions.get(fragment)
            if position is None:
                seen["unresolved"] += 1
                continue
            seen["resolved"] += 1
            window = text[position:position + 500]
            # The shipped judgement, not a second one written here.
            if script_module._toc_title_already_present(label, window):
                continue
            seen["inserted"] += 1
            # Already present but the extractor disagreed? That is the defect.
            # Compare on readings-of-the-page rather than exact characters, the
            # same normalisation the extractor uses, so this reports what a
            # listener would hear rather than what a diff would show.
            loose = script_module._normalize_toc_label(label)
            if loose and loose in script_module._normalize_toc_label(window):
                seen["duplicated"] += 1
            seen["insertions"].append(label)
        return original(text, anchor_positions, toc_targets)

    script_module._insert_epub_toc_titles = spy
    try:
        characters = len(script_module.extract_epub_text(path))
    finally:
        script_module._insert_epub_toc_titles = original
    seen["characters"] = characters
    return seen


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", nargs="*", default=None,
                    help="EPUB paths (default: everything in app/uploads)")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "epub_extraction_fidelity.json"))
    args = ap.parse_args()

    books = args.books or sorted(glob.glob(os.path.join(APP, "uploads", "*.epub")))
    if not books:
        sys.exit("no EPUBs given and none in app/uploads")

    rows, totals = [], {"resolved": 0, "unresolved": 0,
                        "inserted": 0, "duplicated": 0}
    print(f"{'book':40}{'resolved':>10}{'unres':>7}{'inserted':>10}{'dup':>6}")
    for path in books:
        result = measure(path)
        for key in totals:
            totals[key] += result[key]
        rows.append({"book": os.path.basename(path), **result})
        print(f"{os.path.basename(path)[:38]:40}{result['resolved']:>10}"
              f"{result['unresolved']:>7}{result['inserted']:>10}"
              f"{result['duplicated']:>6}")

    print(f"\n{'TOTAL':40}{totals['resolved']:>10}{totals['unresolved']:>7}"
          f"{totals['inserted']:>10}{totals['duplicated']:>6}")
    document = {"books": len(books), "totals": totals, "results": rows}
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    from utils import atomic_json_write
    atomic_json_write(document, args.out)
    print(f"\nwrote {args.out}")
    # The goal's target is 0 duplicated and 0 dropped. Say so in the exit code
    # so a chain can gate on it instead of a human reading the table.
    return 1 if totals["duplicated"] or totals["unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
