"""Rebuild PDNC gold fixtures with a context window that contains the answer.

The shipped fixtures carry ~400 characters either side of each quote. Measured
by pdnc_context_audit.py over three books, that window contains the speaker's
name for 68.5% of EXPLICIT quotes - the category PDNC defines as "introduced by
a named mention", and which the 2026 state of the art scores 99.3% on. At 3200
characters it contains it for 98.2%.

Our own arm scores 64.8% on explicit quotes when the name is visible and 26.9%
when it is not, so roughly a third of that category was being asked a question
its context could not answer.

This writes NEW fixtures beside the originals rather than editing them: the
old numbers stay comparable, and the two can be run as arms of one experiment.
Everything else - ids, speakers, aliases, roster, quote types - is copied
verbatim from the shipped fixture, so the only variable is the window.
"""
import argparse
import ast
import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

FIXTURES = os.path.join(REPO, "app", "fixtures")
# PDNC's directory name differs from the fixture stem; map what we ship.
BOOKS = {
    "attribution_gold_pdnc_prideandprejudice": "PrideAndPrejudice",
    "attribution_gold_pdnc_theawakening": "TheAwakening",
    "attribution_gold_pdnc_thesignofthefour": "TheSignOfTheFour",
}


def spans(pdnc, book):
    """-> {quoteID: [(start, end), ...]}, every part, in order.

    This used to collapse a quotation to (min start, max end) and that outer
    envelope is still what the context window is measured from - see `rebuild`.
    But `"Bah!" said Scrooge, "Humbug!"` is ONE quotation with TWO parts, and
    `said Scrooge` lives in the gap between them, INSIDE the envelope. Contexts
    built outside it therefore contained neither the narration nor any trace of
    it, and `line` is the joined quote text with the narration already removed.

    Measured on the 2,494 scored rows before this changed: 31.3% of quotations
    are multi-part, and among EXPLICIT ones the annotator's own referring
    expression was absent from everything the model saw 69.1% of the time -
    246 of 356 - against 1.6% for single-part quotes. It cost 11.0 points.
    """
    out = {}
    with open(os.path.join(pdnc, book, "quotation_info.csv"),
              encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            pairs = ast.literal_eval(row["quoteByteSpans"])
            out[row["quoteID"]] = sorted((int(a), int(b)) for a, b in pairs)
    return out


def envelope(parts):
    """-> (first start, last end), which is what the window is measured from."""
    return parts[0][0], parts[-1][1]


def inner_narration(text, parts):
    """-> the narration BETWEEN a quotation's parts, in order, or ''.

    Empty for a single-part quotation, which is the common case and must stay
    indistinguishable from a quotation whose parts happen to abut.
    """
    return "".join(text[parts[i][1]:parts[i + 1][0]]
                   for i in range(len(parts) - 1))


def rebuild(fixture_path, pdnc, book, window):
    with open(fixture_path, encoding="utf-8") as handle:
        doc = json.load(handle)
    with open(os.path.join(pdnc, book, "novel_text.txt"),
              encoding="utf-8") as handle:
        text = handle.read()
    located = spans(pdnc, book)

    # THE ID MAPPING IS POSITIONAL AND EXACT. Fixture ids are
    # "<Book>-00042" and PDNC's are "Q42"; verified 2026-08-20 across all three
    # books - 2,494 entries, every one id-matched with whitespace-identical
    # quote text. An earlier version fell back to searching the novel for the
    # quote verbatim when an id lookup missed, which failed on a third of
    # Pride and Prejudice because the stored line normalises whitespace
    # differently, and silently left those entries with their NARROW context.
    # A fixture that is a mixture of window sizes would confound the very
    # comparison it exists to make, so a miss is now fatal.
    widened, unlocated = 0, []
    for entry in doc["entries"]:
        index = entry["id"].rsplit("-", 1)[-1]
        key = "Q%d" % int(index)
        if key not in located:
            unlocated.append(entry["id"])
            continue
        parts = located[key]
        start, end = envelope(parts)
        # THESE THREE FIELDS MUST NOT MOVE. Every number in the ledger was
        # measured against them, so `inner_narration` is ADDITIVE: a new field
        # that a new prompt variant can read, leaving the control arm and every
        # prior comparison byte-identical. test_split_quote_repair.py asserts
        # a regeneration does not disturb them.
        entry["prev_context"] = text[max(0, start - window):start]
        entry["next_context"] = text[end:end + window]
        entry["pdnc_quote_id"] = key
        entry["inner_narration"] = inner_narration(text, parts)
        widened += 1
    if unlocated:
        raise SystemExit(
            "%d of %d entries could not be located in %s (%s...). Refusing to "
            "write a fixture whose entries have different window sizes."
            % (len(unlocated), len(doc["entries"]), book, unlocated[:3]))

    doc["context_chars"] = window
    doc["rebuilt_from"] = "%s/novel_text.txt" % book
    doc["provenance"] = provenance(__file__, None, window=window, book=book)
    return doc, widened


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pdnc", default=os.path.join(
        REPO, "ab_test_runtime", "pdnc", "data"))
    parser.add_argument("--window", type=int, default=3200)
    parser.add_argument("--suffix", default=None,
                        help="default: _w<window>")
    args = parser.parse_args()

    suffix = args.suffix or "_w%d" % args.window
    for stem, book in sorted(BOOKS.items()):
        source = os.path.join(FIXTURES, stem + ".json")
        if not os.path.exists(source):
            print("  skip %s (no fixture)" % stem)
            continue
        doc, widened = rebuild(source, os.path.abspath(args.pdnc),
                               book, args.window)
        out = os.path.join(FIXTURES, stem + suffix + ".json")
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=1, ensure_ascii=False)
        print("  %-48s %4d entries widened to %d chars"
              % (os.path.basename(out), widened, args.window))


if __name__ == "__main__":
    main()
