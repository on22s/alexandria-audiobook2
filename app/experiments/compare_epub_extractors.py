"""Is our EPUB extractor losing or mangling text that another one keeps?

WHY ASK. index18 sat in the corpus for weeks with 6,662 replacement characters
and not one quotation mark, and 32 experiment artifacts were built on it. Our
own extractor re-reads the same EPUB with zero replacements and 1,406 quote
pairs, so THAT file did not come from this code path - but "our extractor is
fine on one book" is not a finding, it is a sample of one.

WHAT IS COMPARED. Not prettiness: the properties that decided real results
here.

  replacement characters   what destroyed index18
  quote marks              the dialogue map, and therefore attribution, is
                           built from them
  paragraph breaks         spans may not cross one; without them the map
                           silently merges narration into speech
  length and token recall  a quiet extractor that drops a chapter looks
                           cleaner than one that keeps everything

Text is never printed, only counted: these are the user's own purchased books.
"""
import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from experiments.provenance import provenance  # noqa: E402


def ours(path):
    from routers.script import extract_epub_text
    return extract_epub_text(path)


def ebooklib_soup(path, parser="lxml"):
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    book = epub.read_epub(path, options={"ignore_ncx": True})
    parts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), parser)
        # Block elements, so paragraph structure survives - the span map
        # depends on blank lines and a naive get_text() destroys them.
        # LEAF BLOCKS ONLY. Matching a div AND the p inside it emits the same
        # text twice - the first run of this reported ebooklib producing
        # 949,038 chars against our 477,757, an exact doubling that looked
        # like our extractor dropping half the book.
        names = ["p", "div", "h1", "h2", "h3", "h4", "li", "blockquote"]
        blocks = [b for b in soup.find_all(names)
                  if not b.find(names)]
        text = "\n\n".join(b.get_text(" ", strip=True) for b in blocks) if blocks \
            else soup.get_text("\n\n", strip=True)
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


EXTRACTORS = {
    "ours_stdlib": ours,
    "ebooklib_lxml": lambda p: ebooklib_soup(p, "lxml"),
    "ebooklib_htmlparser": lambda p: ebooklib_soup(p, "html.parser"),
}


def measure(text):
    words = re.findall(r"[A-Za-z']{2,}", text.lower())
    return {
        "chars": len(text),
        "replacement_chars": text.count("�"),
        "quote_pairs": min(text.count("“"), text.count("”")) + text.count('"') // 2,
        "apostrophes": text.count("’") + text.count("'"),
        "paragraph_breaks": text.count("\n\n"),
        "words": len(words),
        "vocab": len(set(words)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("epubs", nargs="+")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "epub_extractor_comparison.json"))
    args = ap.parse_args()

    results = []
    for path in args.epubs:
        row = {"book": os.path.basename(path), "extractors": {}}
        texts = {}
        for name, fn in EXTRACTORS.items():
            try:
                text = fn(path)
                texts[name] = text
                row["extractors"][name] = measure(text)
            except Exception as exc:                      # noqa: BLE001
                row["extractors"][name] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        # WHAT ONE KEEPS AND ANOTHER DROPS. Vocabulary overlap catches a lost
        # chapter, which every per-extractor count above would call "cleaner".
        if len(texts) > 1:
            base = "ours_stdlib" if "ours_stdlib" in texts else sorted(texts)[0]
            base_vocab = set(re.findall(r"[a-z']{4,}", texts[base].lower()))
            row["vocab_missing_from_ours"] = {
                name: len(set(re.findall(r"[a-z']{4,}", other.lower())) - base_vocab)
                for name, other in texts.items() if name != base}
        results.append(row)
        print(f"\n{row['book']}")
        for name, m in row["extractors"].items():
            if "error" in m:
                print(f"  {name:22s} {m['error']}")
            else:
                print(f"  {name:22s} {m['chars']:8d} chars  {m['replacement_chars']:5d} bad  "
                      f"{m['quote_pairs']:5d} quotes  {m['paragraph_breaks']:6d} paras  "
                      f"{m['vocab']:6d} vocab")
        if "vocab_missing_from_ours" in row:
            print(f"  words present elsewhere but not in ours: "
                  f"{row['vocab_missing_from_ours']}")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"status": "complete", "candidates_considered": len(args.epubs),
                   "results": results, "provenance": provenance(__file__, args)},
                  handle, ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
