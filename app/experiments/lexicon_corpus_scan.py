"""Build a pronunciation-lexicon candidate list from a whole light-novel library.

WHY A LIBRARY AND NOT THE SHIPPED BOOKS. Discovery over the 82 saved scripts
found 29 candidates, which is enough to prove the method and too few to build
a lexicon that transfers. A term appearing in one series might be a typo; a
term appearing in forty is vocabulary the genre uses, and worth a respelling
that every future book inherits.

WHAT IT RECORDS, AND WHAT IT DELIBERATELY DOES NOT. Word statistics only:
the term, how often it occurs, how many books and how many series contain it,
its kana reading, and what the Japanese dictionary calls it. NO SENTENCES ARE
STORED. The output is a frequency table about vocabulary, not an extract of
anyone's book, and it is small enough to read.

THE TEST, unchanged from discover_foreign_terms: present in a Japanese
dictionary AND absent from an English one, because `same` is サメ and `made`
is まで and both are also English. Plus the three false-positive families that
the smaller scan turned up - contraction tails, dropped-g dialect, and
fragments of names.

BOOKS ARE DEDUPLICATED BY CONTENT. The two drives are expected to overlap, and
counting a book twice would silently double the evidence for whatever it
contains. Hashing the extracted text rather than the file means a re-download
or a different EPUB build of the same book still counts once.

RESUMABLE, because this reads thousands of files and will be interrupted. The
checkpoint holds per-book counts, so a rerun skips what it has already read
and nothing is recounted.

SERIES ARE INFERRED FROM THE DIRECTORY, which is how the library is laid out.
A term in 30 books of one series is weaker evidence than a term in 30 books
across 12 series, and the output separates the two so that judgement is
possible later rather than baked in now.
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from discover_foreign_terms import build_detector, CONTRACTION_TAILS  # noqa: E402


def extract(path):
    """-> plain text of one EPUB, reusing the shipped extractor."""
    from routers.script import extract_epub_text
    return extract_epub_text(path)


def series_of(path, roots):
    """The directory under the library root, used as a series label."""
    for root in roots:
        if path.startswith(root):
            rest = os.path.relpath(path, root)
            return rest.split(os.sep)[0]
    return os.path.basename(os.path.dirname(path))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = every book")
    ap.add_argument("--min-books", type=int, default=3,
                    help="a term must appear in at least this many books")
    ap.add_argument("--checkpoint", default=os.path.join(
        REPO, "ab_test_runtime", "lexicon_scan", "checkpoint.json"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_corpus_candidates.json"))
    args = ap.parse_args()

    roots = [os.path.abspath(r) for r in args.roots]
    books = []
    for root in roots:
        books.extend(sorted(glob.glob(os.path.join(root, "**", "*.epub"),
                                      recursive=True)))
    if args.limit:
        books = books[:args.limit]
    if not books:
        sys.exit("no EPUBs found under those roots")

    is_foreign, problem = build_detector()
    if problem:
        sys.exit(problem)

    os.makedirs(os.path.dirname(args.checkpoint), exist_ok=True)
    state = {"done": {}, "hashes": {}}
    if os.path.exists(args.checkpoint):
        try:
            with open(args.checkpoint, encoding="utf-8") as handle:
                state = json.load(handle)
        except ValueError:
            pass
    done, hashes = state.get("done", {}), state.get("hashes", {})

    started = time.time()
    read = skipped = duplicate = failed = 0
    for index, path in enumerate(books, 1):
        if path in done:
            skipped += 1
            continue
        try:
            text = extract(path)
        except Exception:                                   # noqa: BLE001
            failed += 1
            done[path] = {"error": True}
            continue
        digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if digest in hashes:
            duplicate += 1
            done[path] = {"duplicate_of": hashes[digest]}
            continue
        hashes[digest] = os.path.basename(path)
        counts = collections.Counter()
        for word in re.findall(r"[A-Za-z]{4,}", text):
            lowered = word.lower()
            if lowered in CONTRACTION_TAILS:
                continue
            if is_foreign(word):
                counts[lowered] += 1
        done[path] = {"series": series_of(path, roots), "terms": dict(counts)}
        read += 1
        if read % 25 == 0:
            rate = (time.time() - started) / max(read, 1)
            left = (len(books) - index) * rate
            print(f"  {index}/{len(books)}  read={read} dup={duplicate} "
                  f"fail={failed}  {rate:.2f}s/book  ~{left/60:.0f} min left")
            with open(args.checkpoint, "w", encoding="utf-8") as handle:
                json.dump({"done": done, "hashes": hashes}, handle)

    with open(args.checkpoint, "w", encoding="utf-8") as handle:
        json.dump({"done": done, "hashes": hashes}, handle)

    # ---- aggregate: term -> occurrences, books, series
    occurrences = collections.Counter()
    book_count = collections.Counter()
    series_for = collections.defaultdict(set)
    scanned = 0
    for record in done.values():
        if "terms" not in record:
            continue
        scanned += 1
        for term, n in record["terms"].items():
            occurrences[term] += n
            book_count[term] += 1
            series_for[term].add(record.get("series") or "?")

    import romkan
    from sudachipy import Dictionary
    tokenizer = Dictionary(dict="core").create()

    candidates = []
    for term, n in occurrences.items():
        if book_count[term] < args.min_books:
            continue
        kana = romkan.to_katakana(term)
        morphemes = tokenizer.tokenize(kana)
        pos = "/".join(x for x in morphemes[0].part_of_speech()[:4]
                       if x != "*") if len(morphemes) == 1 else "?"
        candidates.append({
            "term": term,
            "occurrences": n,
            "books": book_count[term],
            "series": len(series_for[term]),
            "kana": kana,
            "pos": pos,
            # A term in many series is genre vocabulary; one in many books of a
            # single series is that series' own word. Both are worth a
            # respelling, for different reasons - so rank by neither alone.
            "spread": round(len(series_for[term]) / max(book_count[term], 1), 3),
        })
    candidates.sort(key=lambda c: (-c["series"], -c["books"], -c["occurrences"]))

    document = {
        "roots": roots,
        "epubs_found": len(books),
        "books_scanned": scanned,
        "duplicates_skipped": sum(1 for r in done.values() if "duplicate_of" in r),
        "failed": sum(1 for r in done.values() if r.get("error")),
        "min_books": args.min_books,
        "note": "word statistics only; no sentences are stored",
        "candidates": candidates,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
    print(f"\nscanned {scanned} unique books, {len(candidates)} candidates "
          f"in >= {args.min_books} books")
    for c in candidates[:25]:
        print(f"   {c['occurrences']:7} occ  {c['books']:4} books  "
              f"{c['series']:3} series  {c['term']:16} {c['kana']}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
