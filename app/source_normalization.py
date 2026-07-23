"""Explicit, auditable normalization for known source-text corruption."""

import re


KNOWN_SOURCE_CORRUPTIONS = {"саге": "care", "пар": "nap"}
_KNOWN_RE = re.compile("|".join(re.escape(value) for value in KNOWN_SOURCE_CORRUPTIONS),
                       re.IGNORECASE)
_ILLUSTRATION_CAPTION_RE = re.compile(
    r"Illustration from Volume\s+\d+\s*,\s*coloring by\s+[^\n]{1,100}?\s*"
    r"\(source\)\s*", re.IGNORECASE)


def normalize_known_source_corruptions(text):
    """Return normalized text and location evidence without mutating its source file."""
    changes = []

    def replace(match):
        before = match.group(0)
        after = KNOWN_SOURCE_CORRUPTIONS[before.casefold()]
        if before[:1].isupper():
            after = after.capitalize()
        offset = match.start()
        line = text.count("\n", 0, offset) + 1
        line_start = text.rfind("\n", 0, offset) + 1
        changes.append({"offset": offset, "line": line, "column": offset - line_start + 1,
                        "before": before, "after": after})
        return after

    normalized = _KNOWN_RE.sub(replace, text)

    def remove_caption(match):
        offset = match.start()
        line = normalized.count("\n", 0, offset) + 1
        line_start = normalized.rfind("\n", 0, offset) + 1
        changes.append({"offset": offset, "line": line,
                        "column": offset - line_start + 1,
                        "before": match.group(0), "after": "",
                        "rule": "illustration_caption"})
        return ""

    return _ILLUSTRATION_CAPTION_RE.sub(remove_caption, normalized), changes


# Cyrillic letters visually identical to Latin ones in upright fonts, per the
# Unicode TR39 confusables data (https://www.unicode.org/Public/security/latest/
# confusables.txt), vendored as a small explicit dict rather than a dependency
# so every possible rewrite stays enumerable and auditable.
_HOMOGLYPH_MAP = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ѕ": "s", "ј": "j",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I",
    "Ј": "J",
    # Italic-form confusables observed in this corpus's actual corrupted
    # sources (KNOWN_SOURCE_CORRUPTIONS: "саге" -> "care", "пар" -> "nap").
    "г": "r", "п": "n",
}
_CYRILLIC_CHAR_RE = re.compile(r"[Ѐ-ӿ]")
_CYRILLIC_WORD_RE = re.compile(r"\w*[Ѐ-ӿ]\w*", re.UNICODE)
# A genuinely bilingual/Cyrillic text must never be rewritten; stray OCR
# corruption in a Latin book stays far below this share of letters.
MAX_HOMOGLYPH_CYRILLIC_RATIO = 0.005


def normalize_homoglyph_words(text):
    """Map whole words of Cyrillic lookalike characters back to Latin,
    returning normalized text and location evidence without mutating the
    source file. Complements normalize_known_source_corruptions (which runs
    first and handles exact known words): a word is rewritten only when the
    document is overwhelmingly Latin and every Cyrillic character in the
    word has a homoglyph mapping - one unmappable character leaves the whole
    word untouched."""
    letter_count = sum(1 for char in text if char.isalpha())
    if not letter_count:
        return text, []
    cyrillic_count = sum(1 for char in text if _CYRILLIC_CHAR_RE.match(char))
    if not cyrillic_count or cyrillic_count / letter_count >= MAX_HOMOGLYPH_CYRILLIC_RATIO:
        return text, []

    changes = []

    def replace(match):
        word = match.group(0)
        if any(char not in _HOMOGLYPH_MAP
               for char in _CYRILLIC_CHAR_RE.findall(word)):
            return word
        after = "".join(_HOMOGLYPH_MAP.get(char, char) for char in word)
        offset = match.start()
        line = text.count("\n", 0, offset) + 1
        line_start = text.rfind("\n", 0, offset) + 1
        changes.append({"offset": offset, "line": line, "column": offset - line_start + 1,
                        "before": word, "after": after, "rule": "homoglyph"})
        return after

    return _CYRILLIC_WORD_RE.sub(replace, text), changes


_FRONT_MATTER_ANCHOR = re.compile(
    r"Original (?:Web Novel|Light Novel) Chapter\s*[―—-]\s*(?:In)?[Cc]omplete\.\s*\n+"
    r"Original Translation by [^\n]+\.\s*\n+"
)


def strip_known_front_matter(text):
    """Strip a known fan-compiler's non-narrative front matter (translator's
    note + table of contents) when present, returning the story text and
    evidence of what was removed without mutating the source file.

    Scoped to one observed, stable compiler template (confirmed across 5
    "wn" uploads): a "Manifesto." translator's essay and chapter listing,
    ending right before the first "Original ... Chapter - Complete." /
    "Original Translation by ..." marker pair, which is always immediately
    followed by the real chapter 1 prose. This content isn't dialogue or
    narration and was measured live to break chunk generation (near-zero
    recall no matter how many times retried or split) since the annotation
    model has no idea how to handle it. Returns the text unchanged (and
    None) whenever the shape doesn't match, rather than guessing.
    """
    if not text.lstrip("﻿ \t\r\n").startswith("Manifesto."):
        return text, None
    match = _FRONT_MATTER_ANCHOR.search(text)
    if not match:
        return text, None
    return text[match.end():], {"removed_chars": match.end(),
                                 "removed_lines": text.count("\n", 0, match.end())}
