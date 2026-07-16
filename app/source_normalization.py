"""Explicit, auditable normalization for known source-text corruption."""

import re


KNOWN_SOURCE_CORRUPTIONS = {"саге": "care"}
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
