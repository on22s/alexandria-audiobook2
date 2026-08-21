"""Does supplying the annotator's own evidence make the model right?

PDNC publishes, for every quotation, the `referringExpression` its annotators
used to decide the speaker - "said his lady to him", "cried Elizabeth". Our
fixtures keep `pdnc_quote_id`, so each scored row can be joined back to that
column and asked a question no arm has asked: was the evidence the human used
inside the context window we handed the model, and did having it help?

The question came from a hard-negative mining paper (Moreira et al., NVIDIA)
whose premise is that the hardest cases are the ones where the label and the
evidence disagree. That premise is not checkable here - PDNC ships one
adjudicated speaker per quote and no annotator agreement - but the evidence
column is, and it answers something better.

WHAT THE MATCH IS. Exact substring, after folding to lowercase alphanumerics.
A referring expression present in a different surface form counts as ABSENT, so
the "outside our window" share is an upper bound on true absence and the
measured benefit of presence is, if anything, understated.
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

csv.field_size_limit(10 ** 7)
FIXTURE_PREFIX = "attribution_gold_pdnc_"


def normalise(text):
    return " ".join(re.sub(r"[^0-9a-z ]", " ", (text or "").lower()).split())


def load_raw(pdnc_dir):
    """-> {book folder: {quoteID: row}} from PDNC's published annotations."""
    out = {}
    for path in sorted(glob.glob(os.path.join(pdnc_dir, "*", "quotation_info.csv"))):
        book = os.path.basename(os.path.dirname(path))
        with open(path, encoding="utf-8") as handle:
            out[book] = {r["quoteID"]: r for r in csv.DictReader(handle)}
    return out


def match_book(key, raw):
    """Fixture stems are lowercase and unpunctuated; PDNC folders are not."""
    want = re.sub(r"[^a-z0-9]", "", key.lower())
    for book in raw:
        if re.sub(r"[^a-z0-9]", "", book.lower()) == want:
            return book
    return None


def load_gold(fixtures_dir, raw):
    """-> {(fixture stem, quote id): (pdnc row, entry)} for joinable rows."""
    out, unjoined = {}, 0
    for path in sorted(glob.glob(os.path.join(
            fixtures_dir, FIXTURE_PREFIX + "*.json"))):
        stem = os.path.basename(path)[:-len(".json")]
        key = stem[len(FIXTURE_PREFIX):]
        for suffix in ("_w3200", ""):
            if key.endswith(suffix) and suffix:
                key = key[:-len(suffix)]
                break
        book = match_book(key, raw)
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        for entry in fixture.get("entries") or []:
            row = (raw.get(book) or {}).get(entry.get("pdnc_quote_id")) if book else None
            if row is None:
                unjoined += 1
                continue
            out[(stem, entry["id"])] = (row, entry)
    return out, unjoined


def classify(row, entry):
    """-> True when the annotator's referring expression is in our window."""
    reference = normalise(row.get("referringExpression"))
    if not reference:
        return None
    context = normalise(" ".join((entry.get("prev_context") or "",
                                  entry.get("line") or "",
                                  entry.get("next_context") or "")))
    return reference in context


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--pdnc", default=os.path.join(REPO, "ab_test_runtime", "pdnc", "data"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = load_raw(args.pdnc)
    if not raw:
        raise SystemExit("no quotation_info.csv under %s" % args.pdnc)
    gold, unjoined = load_gold(args.fixtures, raw)

    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    cells = collections.defaultdict(lambda: {"n": 0, "correct": 0})
    no_reference = 0
    scored = 0
    for row in artifact.get("rows") or []:
        book, _, quote = (row.get("id") or "").partition(":")
        joined = gold.get((book, quote))
        if not joined:
            continue
        inside = classify(*joined)
        if inside is None:
            no_reference += 1
            continue
        scored += 1
        cell = cells[(joined[1].get("quote_type") or "?", inside)]
        cell["n"] += 1
        cell["correct"] += 1 if row.get("correct") else 0

    by_type = {}
    for (quote_type, inside), cell in cells.items():
        block = by_type.setdefault(quote_type, {})
        block["inside" if inside else "outside"] = {
            "n": cell["n"],
            "accuracy": round(cell["correct"] / cell["n"], 4) if cell["n"] else None}
    for quote_type, block in by_type.items():
        a, b = block.get("inside"), block.get("outside")
        block["points_from_having_the_evidence"] = (
            round(100 * (a["accuracy"] - b["accuracy"]), 2)
            if a and b and a["accuracy"] is not None and b["accuracy"] is not None
            else None)

    total_in = sum(c["n"] for (t, i), c in cells.items() if i)
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "PDNC's own referringExpression per quotation, joined to our "
                 "scored rows by pdnc_quote_id. Exact substring match after "
                 "folding to lowercase alphanumerics, so a different surface "
                 "form counts as absent and the benefit of presence is, if "
                 "anything, understated",
        "source_artifact": os.path.basename(args.artifact),
        "rows_scored": scored,
        "rows_not_joinable_to_pdnc": unjoined,
        "rows_with_no_referring_expression": no_reference,
        "share_with_evidence_in_window": round(total_in / scored, 4) if scored else None,
        "by_quote_type": by_type,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-11s %-20s %6s %9s" % ("quote type", "annotator evidence", "n", "accuracy"))
    for quote_type in sorted(by_type):
        block = by_type[quote_type]
        for where in ("inside", "outside"):
            cell = block.get(where)
            if cell:
                print("%-11s %-20s %6d %9.3f" % (quote_type, where, cell["n"],
                                                 cell["accuracy"]))
        print("%-11s %-20s %6s %9s" % ("", "-> worth", "",
                                       block["points_from_having_the_evidence"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
