"""Convert ModernBookNLP output into the project's attribution row format."""
import argparse
import json
import os
import pickle
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.booknlp_baseline import (  # noqa: E402
    align_to_gold, parse_booknlp, read_tsv,
)
from experiments.scoring import alias_groups, same_speaker  # noqa: E402


def adapt(quotes_path, entities_path, fixture, id_prefix, extra_aliases=(),
          narrator=None):
    """Align ModernBookNLP outputs and preserve every alignment failure count."""
    raw = parse_booknlp(read_tsv(quotes_path), read_tsv(entities_path))
    matched, unmatched, conflicts = align_to_gold(raw, fixture)
    by_id = {entry["id"]: entry for entry in fixture.get("entries") or []}
    groups = alias_groups(fixture, extra_aliases)
    rows = []
    for item in matched:
        source = by_id[item["id"]]
        predicted = item.get("predicted") or None
        equivalent = bool(predicted) and same_speaker(
            item["expected"], predicted, groups)
        rows.append({
            "id": "%s:%s" % (id_prefix, item["id"]),
            "line": source.get("line"),
            "expected": item["expected"],
            "predicted": item["expected"] if equivalent else predicted,
            "raw_predicted": predicted,
            "correct": equivalent,
            "quote_type": source.get("quote_type"),
            "confidence": None,
            "narrator": narrator,
            "split_alignment": bool(item.get("split")),
        })
    return rows, {"fixture_rows": len(fixture.get("entries") or []),
                  "matched": len(rows), "unmatched": unmatched,
                  "split_speaker_conflicts": conflicts}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--quotes", required=True)
    parser.add_argument("--entities", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--book", help="keep fixture IDs beginning BOOK-")
    parser.add_argument("--id-prefix", help="prefix used by the Qwen artifact")
    parser.add_argument("--char-info",
                        help="PDNC charInfo.dict.pkl supplying canonical aliases")
    parser.add_argument("--narrator", help="known first-person narrator identity")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.fixture, encoding="utf-8") as handle:
        fixture = json.load(handle)
    fixture_stem = os.path.basename(args.fixture)[:-len(".json")]
    if args.book:
        fixture = dict(fixture)
        fixture["entries"] = [entry for entry in fixture.get("entries") or []
                              if entry.get("id", "").startswith(args.book + "-")]
        if not fixture["entries"]:
            raise SystemExit("fixture has no rows for --book %s" % args.book)
    extra_aliases = []
    if args.char_info:
        with open(args.char_info, "rb") as handle:
            character_info = pickle.load(handle)
        extra_aliases = list(character_info.get("id2names", {}).values())
    rows, coverage = adapt(args.quotes, args.entities, fixture,
                           args.id_prefix or args.book or fixture_stem,
                           extra_aliases, args.narrator)
    if not rows:
        raise SystemExit("ModernBookNLP output aligned to zero fixture rows")
    document = {
        "status": "complete", "system": "ModernBookNLP Joint T2000 S512",
        "fixture": fixture_stem, "coverage": coverage, "rows": rows,
        "limitations": [
            "The standalone TSV output does not expose the model's confidence; "
            "confidence-threshold hybrid policies therefore fall back to Qwen.",
            "Accuracy is reported on the full fixture denominator by the hybrid "
            "harness; unmatched rows are never silently removed."],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
    print(json.dumps(coverage, indent=1))


if __name__ == "__main__":
    main()
