"""Explicit, auditable normalization for known source-text corruption."""

import re


KNOWN_SOURCE_CORRUPTIONS = {"саге": "care", "пар": "nap"}
_KNOWN_RE = re.compile("|".join(re.escape(value) for value in KNOWN_SOURCE_CORRUPTIONS),
                       re.IGNORECASE)


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

    return _KNOWN_RE.sub(replace, text), changes


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
