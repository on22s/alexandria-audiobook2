"""Is the mention that names the speaker even inside the context we supply?

WHY THIS EXISTS. The 2026 state of the art reports 99.3% on EXPLICIT quotations
and a one-pass Llama-3-8b reports 94.9% (arXiv 2608.02359, Table 1). This
project's arm gets 52.9% on the same category of the same corpus. That gap is
not a hard problem being lost - explicit quotes are the easy case, and the
field treats them as close to solved.

PDNC defines an explicit quotation as one INTRODUCED BY A NAMED MENTION: the
speaker's name sits beside the quote. So a window containing the quote should
contain the name. On the shipped fixtures it does for only 68.5% of explicit
quotes, and accuracy splits on exactly that - 64.8% when the name is visible
against 26.9% when it is not.

It calls no model. "Can the answer be seen from here" is a property of the
text, and asking an LLM would measure two things at once.
"""
import argparse
import ast
import csv
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

# The corpus is large and lives in the main checkout, not in a development
# worktree (Rule 24). Overridable for that reason.
PDNC = os.path.join(REPO, "ab_test_runtime", "pdnc", "data")


def novel(book):
    with open(os.path.join(PDNC, book, "novel_text.txt"), encoding="utf-8") as h:
        return h.read()


def quotations(book):
    with open(os.path.join(PDNC, book, "quotation_info.csv"),
              encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            spans = ast.literal_eval(row["quoteByteSpans"])
            yield {"id": row["quoteID"],
                   "start": min(s[0] for s in spans),
                   "end": max(s[1] for s in spans),
                   "speaker": row["speaker"],
                   "type": row["quoteType"]}


def aliases_for(book):
    """-> {name upper: {every alias upper}} from PDNC's own character file."""
    table = {}
    with open(os.path.join(PDNC, book, "character_info.csv"),
              encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            names = {row.get("Main Name") or ""}
            raw = row.get("Aliases") or row.get("aliases") or ""
            if raw:
                try:
                    names |= set(ast.literal_eval(raw))
                except (ValueError, SyntaxError):
                    names |= {n.strip() for n in raw.split(";")}
            upper = {n.upper().strip() for n in names if n and n.strip()}
            for name in upper:
                table[name] = upper
    return table


def visible(text, names):
    upper = text.upper()
    return any(re.search(r"\b%s\b" % re.escape(n), upper) for n in names if n)


def audit(book, windows):
    text, table = novel(book), aliases_for(book)
    counts = {w: {} for w in windows}
    for quote in quotations(book):
        names = table.get(quote["speaker"].upper(), {quote["speaker"].upper()})
        for window in windows:
            left = text[max(0, quote["start"] - window):quote["start"]]
            right = text[quote["end"]:quote["end"] + window]
            bucket = counts[window].setdefault(quote["type"], [0, 0])
            bucket[0] += 1
            bucket[1] += visible(left + " " + right, names)
    return counts


def main():
    global PDNC
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pdnc", default=PDNC,
                        help="PDNC data dir (large; lives in the main checkout)")
    parser.add_argument("--books", nargs="+", default=None)
    parser.add_argument("--windows", nargs="+", type=int,
                        default=[400, 800, 1600, 3200])
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "pdnc_context_audit.json"))
    args = parser.parse_args()

    PDNC = os.path.abspath(args.pdnc)
    books = args.books or sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(PDNC, "*"))
        if os.path.exists(os.path.join(p, "quotation_info.csv")))
    totals = {w: {} for w in args.windows}
    for book in books:
        for window, per_type in audit(book, args.windows).items():
            for qt, (n, seen) in per_type.items():
                row = totals[window].setdefault(qt, [0, 0])
                row[0] += n
                row[1] += seen

    print("%-8s %-10s %8s %12s" % ("window", "type", "quotes", "name in view"))
    payload = {}
    for window in args.windows:
        for qt in sorted(totals[window]):
            n, seen = totals[window][qt]
            print("%-8d %-10s %8d %11.1f%%" % (window, qt, n, 100.0 * seen / n))
            payload.setdefault(str(window), {})[qt] = {
                "quotes": n, "name_in_view": seen,
                "pct": round(100.0 * seen / n, 1)}
        print()
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"books": books, "by_window": payload,
                   "note": ("Characters of novel text either side of the "
                            "quote. PDNC defines an explicit quotation as one "
                            "introduced by a named mention, so explicit should "
                            "approach 100% once the window contains it."),
                   "provenance": provenance(__file__, args)},
                  handle, indent=1, ensure_ascii=False)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
