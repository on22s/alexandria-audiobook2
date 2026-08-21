"""Recover the text around a quote by locating it in the book's source.

The PDNC fixtures carry `prev_context` and `next_context`; the light-novel
fixtures carry only the line and its speaker. Every experiment that reads what
surrounds a quote - the Elson trigram, adjacency features, window-width arms -
is therefore free on the 2,494 PDNC rows and needs this step on the other four
books.

MATCHING IS ON ALPHANUMERICS ONLY, and that is not a shortcut. Generation
strips and re-inserts quote marks, translators use " and 『』 in the same
volume, and Gutenberg wraps lines mid-sentence; comparing raw text finds
almost nothing. `retrofit_dialogue_map.py` learned the same thing and matches
quote-stripped prose for the same reason.

A line that appears twice in a book is reported AMBIGUOUS, not resolved to its
first occurrence. Short exclamations repeat - "Ah-ha-ha!!" - and picking the
first one silently attaches the wrong neighbours to a real gold row.
"""
import re

_KEEP = re.compile(r"[0-9a-z]+")


def build_index(source):
    """-> (normalised text, offset map back into `source`)."""
    chars, offsets = [], []
    for match in _KEEP.finditer(source.lower()):
        for position in range(match.start(), match.end()):
            chars.append(source[position].lower())
            offsets.append(position)
    return "".join(chars), offsets


def normalize(line):
    return "".join(_KEEP.findall((line or "").lower()))


def locate(line, normalised, offsets, source, window=200, min_chars=12):
    """-> (prev_context, next_context, status).

    status is one of: located, ambiguous, not_found, too_short. Contexts are
    None unless status is "located".
    """
    key = normalize(line)
    if len(key) < min_chars:
        # A three-character line matches everywhere; refusing is the only
        # honest answer, and saying so keeps it out of the denominator.
        return None, None, "too_short"
    first = normalised.find(key)
    if first < 0:
        return None, None, "not_found"
    if normalised.find(key, first + 1) >= 0:
        return None, None, "ambiguous"
    start = offsets[first]
    end = offsets[first + len(key) - 1] + 1
    return source[max(0, start - window):start], source[end:end + window], "located"
