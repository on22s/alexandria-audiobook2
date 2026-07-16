#!/usr/bin/env python3
"""Build a deterministic, read-only real-book integration corpus manifest."""

import argparse
import hashlib
import os
from pathlib import Path
import re

from routers.script import extract_epub_text
from utils import atomic_json_write


BOOK_EXTENSIONS = {".epub", ".txt"}
CATEGORY_PATTERNS = {
    "dialogue": re.compile(r'[“"][^”"\n]{40,}[”"]'),
    "expressive_punctuation": re.compile(r"(?:—|–|\.\.\.|…|[!?]{2,})"),
    "non_ascii": re.compile(r"[^\x00-\x7f]"),
    "long_sentence": re.compile(r"[^.!?\n]{240,}[.!?]"),
}


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_book(path):
    if path.suffix.lower() == ".epub":
        return extract_epub_text(str(path))
    return path.read_text(encoding="utf-8", errors="replace")


def _passage_around(text, start, target_chars):
    left = max(0, start - target_chars // 3)
    right = min(len(text), left + target_chars)
    passage = re.sub(r"\s+", " ", text[left:right]).strip()
    return passage


def select_passages(text, target_chars=1200):
    """Return one deterministic representative passage per available category."""
    passages = []
    for category, pattern in CATEGORY_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        passage = _passage_around(text, match.start(), target_chars)
        digest = _sha256_bytes(passage.encode("utf-8"))
        if passage:
            passages.append({"category": category, "text": passage, "sha256": digest})
    fallback = _passage_around(text, 0, target_chars)
    digest = _sha256_bytes(fallback.encode("utf-8"))
    if fallback:
        passages.append({"category": "opening_narration", "text": fallback, "sha256": digest})
    return passages


def build_manifest(books_dir, max_books=10, target_chars=1200):
    """Read books without modifying them and return a reproducible corpus manifest."""
    books_dir = Path(books_dir).resolve()
    paths = sorted(
        (path for path in books_dir.iterdir()
         if path.is_file() and path.suffix.lower() in BOOK_EXTENSIONS),
        key=lambda path: path.name.casefold(),
    )[:max_books]
    books = []
    errors = []
    for path in paths:
        try:
            raw = path.read_bytes()
            text = _read_book(path)
            if not text.strip():
                raise ValueError("no readable text")
            passages = select_passages(text, target_chars=target_chars)
            books.append({
                "name": path.name,
                "source_sha256": _sha256_bytes(raw),
                "source_size_bytes": len(raw),
                "text_characters": len(text),
                "passages": passages,
            })
        except Exception as exc:
            errors.append({"name": path.name, "error": f"{type(exc).__name__}: {exc}"})
    return {"schema_version": 1, "books_dir": str(books_dir),
            "books": books, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-books", type=int, default=10)
    parser.add_argument("--target-chars", type=int, default=1200)
    args = parser.parse_args(argv)
    if args.max_books < 1 or args.target_chars < 200:
        parser.error("--max-books must be >= 1 and --target-chars must be >= 200")
    manifest = build_manifest(args.books_dir, args.max_books, args.target_chars)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    atomic_json_write(manifest, args.output)
    print(f"Wrote {len(manifest['books'])} book(s) and {len(manifest['errors'])} error(s) to {args.output}")
    return 0 if manifest["books"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
