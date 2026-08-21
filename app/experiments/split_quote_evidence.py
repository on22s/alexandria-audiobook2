"""A quote split by narration loses its attribution before the model sees it.

`pdnc_gold_rebuild.spans` collapses a quotation's byte spans to their outer
envelope - `min(start), max(end)` - and then builds

    prev_context = text[start - window : start]
    next_context = text[end : end + window]

For a quote split by narration, `"Bah!" said Scrooge, "Humbug!"`, the words
`said Scrooge` sit BETWEEN the two parts, inside [start, end]. They are in
neither context, and `line` is the joined quote text with the narration
removed. The attribution is structurally deleted from the prompt.

31.3% of PDNC quotations are multi-part, so this is not a corner case.

This measures the damage rather than assuming it: how often the annotator's own
referring expression survives into what the model sees, split by whether the
quotation is one part or several, and what that costs in accuracy.
"""
import argparse
import ast
import collections
import csv
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.annotator_evidence import load_raw, match_book, normalise  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

csv.field_size_limit(10 ** 7)
PREFIX = "attribution_gold_pdnc_"


def part_count(row):
    """-> how many pieces PDNC recorded this quotation in."""
    try:
        parts = ast.literal_eval(row.get("subQuotationList") or "[]")
    except (ValueError, SyntaxError):
        return 1
    return len(parts) if isinstance(parts, list) and parts else 1


def evidence_location(row, entry):
    """-> where the annotator's referring expression survives, if anywhere."""
    reference = normalise(row.get("referringExpression"))
    if not reference:
        return None
    if reference in normalise(entry.get("line")):
        return "line"
    surrounding = normalise(" ".join((entry.get("prev_context") or "",
                                      entry.get("next_context") or "")))
    return "context" if reference in surrounding else "absent"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--pdnc", default=os.path.join(REPO, "ab_test_runtime", "pdnc", "data"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = load_raw(args.pdnc)
    gold = {}
    for path in sorted(glob.glob(os.path.join(args.fixtures, PREFIX + "*.json"))):
        stem = os.path.basename(path)[:-len(".json")]
        key = stem[len(PREFIX):]
        for suffix in ("_w3200",):
            if key.endswith(suffix):
                key = key[:-len(suffix)]
        book = match_book(key, raw)
        if not book:
            continue
        with open(path, encoding="utf-8") as handle:
            for entry in json.load(handle).get("entries") or []:
                row = raw[book].get(entry.get("pdnc_quote_id"))
                if row is not None:
                    gold[(stem, entry["id"])] = (row, entry)

    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    acc = collections.defaultdict(lambda: [0, 0])
    loc = collections.Counter()
    multi = single = 0
    for row in artifact.get("rows") or []:
        stem, _, quote = (row.get("id") or "").partition(":")
        joined = gold.get((stem, quote))
        if not joined:
            continue
        pdnc_row, entry = joined
        form = "multi-part" if part_count(pdnc_row) > 1 else "single"
        multi += form == "multi-part"
        single += form == "single"
        cell = acc[(entry.get("quote_type"), form)]
        cell[0] += 1
        cell[1] += 1 if row.get("correct") else 0
        if entry.get("quote_type") == "Explicit":
            where = evidence_location(pdnc_row, entry)
            if where:
                loc[(form, where)] += 1

    by_type = {}
    for (quote_type, form), (n, correct) in acc.items():
        by_type.setdefault(quote_type, {})[form] = {
            "n": n, "accuracy": round(correct / n, 4) if n else None}
    for block in by_type.values():
        a, b = block.get("single"), block.get("multi-part")
        block["points_lost_when_split"] = (
            round(100 * (a["accuracy"] - b["accuracy"]), 2)
            if a and b and a["accuracy"] is not None and b["accuracy"] is not None
            else None)

    explicit = {}
    for form in ("single", "multi-part"):
        total = sum(v for (f, w), v in loc.items() if f == form)
        explicit[form] = {
            "n": total,
            **{w: {"n": loc[(form, w)],
                   "share": round(loc[(form, w)] / total, 4) if total else None}
               for w in ("line", "context", "absent")}}

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "what a quotation split by narration costs, measured against "
                 "PDNC's own referringExpression. Nothing was re-run",
        "source_artifact": os.path.basename(args.artifact),
        "quotations_single_part": single,
        "quotations_multi_part": multi,
        "multi_part_share": round(multi / (multi + single), 4) if multi + single else None,
        "accuracy_by_quote_type_and_form": by_type,
        "explicit_evidence_survival": explicit,
        "cause": "pdnc_gold_rebuild.spans collapses a quotation to "
                 "min(start), max(end); the narration between parts falls "
                 "inside that envelope and so into neither context, while "
                 "`line` holds the joined quote without it",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("multi-part quotations: %d of %d (%.1f%%)\n"
          % (multi, multi + single, 100 * doc["multi_part_share"]))
    print("  %-11s %-12s %6s %9s" % ("quote type", "form", "n", "accuracy"))
    for quote_type in sorted(by_type):
        for form in ("single", "multi-part"):
            cell = by_type[quote_type].get(form)
            if cell:
                print("  %-11s %-12s %6d %9.3f" % (quote_type, form, cell["n"],
                                                   cell["accuracy"]))
        print("  %-11s %-12s %6s %9s" % ("", "-> costs", "",
                                         by_type[quote_type]["points_lost_when_split"]))
    print("\nExplicit rows, where the annotator's evidence survives:")
    for form in ("single", "multi-part"):
        block = explicit[form]
        print("  %-12s n=%-4d line %-5d context %-5d ABSENT %-5d (%.1f%%)"
              % (form, block["n"], block["line"]["n"], block["context"]["n"],
                 block["absent"]["n"], 100 * (block["absent"]["share"] or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
