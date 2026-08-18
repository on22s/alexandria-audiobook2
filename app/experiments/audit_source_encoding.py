"""Which corpus files are too damaged to draw conclusions from?

index18 carries 6,662 replacement characters - 1.4% of its text, against a
gate of 0.5% - and not one quotation mark or apostrophe survives. The bytes are
literally EF BF BD, the UTF-8 encoding of U+FFFD, so the file was WRITTEN after
a lossy decode: the original characters are gone and no amount of re-reading
this file recovers them. Only re-extracting from the EPUB does, and that EPUB
is not on this machine.

That matters beyond one book. 32 experiment artifacts name index18, and
GOALS.md quotes per-book figures from it in the four-book set the method
findings rest on. A book whose dialogue markup was destroyed before any model
saw it cannot be evidence about segmentation or attribution - and it has been
described here as "the hard book that exposed five blockers", which is a
statement about our extraction rather than about the book.

This audits every input, names the artifacts that cite a failing one, and exits
non-zero so a queue surfaces it rather than continuing over it.
"""
import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from script_preflight import MAX_REPLACEMENT_SHARE  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

INPUTS = os.path.join(REPO, "ab_test_runtime", "results",
                      "collect_all_20260722-155801", "inputs")
EXPERIMENTS = os.path.join(REPO, "ab_test_runtime", "experiments")


def replacement_share(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return text.count("�"), len(text)


def artifacts_naming(book):
    """Artifacts whose FILENAME names this book - the ones a reader would find."""
    return sorted(os.path.basename(p) for p in
                  glob.glob(os.path.join(EXPERIMENTS, f"*{book}*")))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inputs", default=INPUTS)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "source_encoding_audit.json"))
    args = ap.parse_args()

    rows = []
    for path in sorted(glob.glob(os.path.join(args.inputs, "*.txt"))):
        book = os.path.basename(path)[:-4]
        count, length = replacement_share(path)
        share = count / max(length, 1)
        failed = share > MAX_REPLACEMENT_SHARE
        row = {"book": book, "replacements": count, "chars": length,
               "share": round(share, 5), "passes_gate": not failed}
        if failed:
            row["artifacts_naming_this_book"] = artifacts_naming(book)
            row["remedy"] = ("re-extract from the EPUB; the damage is baked "
                             "into this file and cannot be repaired by "
                             "re-reading it")
        rows.append(row)
        state = "REFUSED" if failed else "ok"
        print(f"  {book:22s} {count:6d} replacements {share:7.3%}  {state}")

    bad = [r for r in rows if not r["passes_gate"]]
    doc = {"status": "complete", "candidates_considered": len(rows),
           "gate": MAX_REPLACEMENT_SHARE, "results": rows,
           "provenance": provenance(__file__, args)}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=1)

    if bad:
        print(f"\n{len(bad)} book(s) fail the encoding gate:")
        for row in bad:
            print(f"  {row['book']}: {len(row['artifacts_naming_this_book'])} "
                  f"artifacts name it, and every one is confounded")
        # Non-zero on purpose: this is a stop, not a note. Continuing past it
        # produces more measurements of an encoding bug.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
