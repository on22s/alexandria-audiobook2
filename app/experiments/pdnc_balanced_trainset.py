"""Build author-held-out, speaker-balanced PDNC adapter training data."""
import argparse
import ast
import collections
import csv
import json
import os
import random

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ROOT = os.path.join(REPO, "ab_test_runtime", "pdnc")
SPECIAL = {"UNKNOWN", "UNKNOWABLE", "UNNAMED", "NOT_DIALOGUE"}


def load_author_map(index_path):
    """Return folder-to-author codes from PDNC's canonical novel index."""
    with open(index_path, encoding="utf-8-sig", newline="") as handle:
        return {row["Folder Name"]: row["Author Code"]
                for row in csv.DictReader(handle)}


def load_characters(path):
    """Return canonical speaker metadata and a roster for one novel."""
    speakers, roster = {}, []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("Main Name") or "").strip()
            if not name:
                continue
            roster.append(name.upper())
            for alias in [name] + list(ast.literal_eval(row.get("Aliases") or "[]")):
                speakers[str(alias).strip().upper()] = {
                    "canonical": name.upper(),
                    "category": (row.get("Category") or "unknown").lower(),
                }
    return speakers, sorted(set(roster))


def read_rows(folder, context_chars):
    """Return adapter rows with character category attached for balancing."""
    speakers, roster = load_characters(os.path.join(folder, "character_info.csv"))
    text = open(os.path.join(folder, "novel_text.txt"), encoding="utf-8").read()
    rows = []
    with open(os.path.join(folder, "quotation_info.csv"),
              encoding="utf-8-sig", newline="") as handle:
        for number, raw in enumerate(csv.DictReader(handle)):
            line = (raw.get("quoteText") or "").strip()
            info = speakers.get((raw.get("speaker") or "").strip().upper())
            if not line or not info or info["canonical"] in SPECIAL:
                continue
            try:
                spans = ast.literal_eval(raw.get("quoteByteSpans") or "[]")
                start, end = min(x[0] for x in spans), max(x[1] for x in spans)
            except (SyntaxError, ValueError, TypeError):
                continue
            rows.append({
                "segment_index": number, "roster": roster,
                "context": [
                    {"type": "NARRATOR", "text": text[max(0, start-context_chars):start].strip()},
                    {"type": "SPOKEN", "text": line, "target": True},
                    {"type": "NARRATOR", "text": text[end:end+context_chars].strip()},
                ],
                "line": line, "teacher": info["canonical"],
                "speaker_category": info["category"],
                "quote_structure": "split" if len(spans) > 1 else "continuous",
            })
    return rows


def balanced_sample(rows, limit, per_speaker, rng):
    """Round-robin categories and speakers, refusing one dominant character."""
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for row in rows:
        key = (row["speaker_category"], row.get("quote_structure", "continuous"))
        groups[key][row["teacher"]].append(row)
    for speakers in groups.values():
        for values in speakers.values():
            rng.shuffle(values)
    selected, counts = [], collections.Counter()
    categories = [key for category in ("major", "intermediate", "minor", "unknown")
                  for structure in ("split", "continuous")
                  for key in [(category, structure)] if key in groups]
    while len(selected) < limit:
        progressed = False
        for category in categories:
            for speaker in sorted(groups[category]):
                values = groups[category][speaker]
                if values and counts[speaker] < per_speaker:
                    selected.append(values.pop())
                    counts[speaker] += 1
                    progressed = True
                    if len(selected) == limit:
                        break
            if len(selected) == limit:
                break
        if not progressed:
            break
    rng.shuffle(selected)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--exclude-author", action="append", default=[])
    parser.add_argument("--per-novel", type=int, default=200)
    parser.add_argument("--per-speaker", type=int, default=25)
    parser.add_argument("--context-chars", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    author_map = load_author_map(os.path.join(args.root, "PDNC-Novel-Index.csv"))
    excluded = set(args.exclude_author)
    rng = random.Random(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)
    manifest, total = [], 0
    for book, author in sorted(author_map.items()):
        if author in excluded:
            manifest.append({"book": book, "author": author, "status": "excluded"})
            continue
        folder = os.path.join(args.root, "data", book)
        rows = balanced_sample(read_rows(folder, args.context_chars),
                               args.per_novel, args.per_speaker, rng)
        path = os.path.join(args.out_dir, "train__pdnc_%s.jsonl" % book)
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                row["book"] = "pdnc_" + book
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        categories = collections.Counter(row["speaker_category"] for row in rows)
        structures = collections.Counter(row["quote_structure"] for row in rows)
        speakers = collections.Counter(row["teacher"] for row in rows)
        manifest.append({"book": book, "author": author, "status": "train",
                         "rows": len(rows), "categories": dict(categories),
                         "quote_structures": dict(structures),
                         "max_rows_per_speaker": max(speakers.values(), default=0)})
        total += len(rows)
    document = {"seed": args.seed, "excluded_authors": sorted(excluded),
                "per_novel": args.per_novel, "per_speaker": args.per_speaker,
                "total_rows": total, "books": manifest}
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1)
    print(json.dumps({"total_rows": total,
                      "training_books": sum(x["status"] == "train" for x in manifest),
                      "excluded_books": sum(x["status"] == "excluded" for x in manifest)}, indent=1))


if __name__ == "__main__":
    main()
