"""Token / n-gram / multiset-recall math shared by the single-pass quality gate
(chunk_quality) and the three-pass segment gate (pass_quality). Kept in one
module so both score source-text fidelity identically (no duplicated decision
logic)."""

import re
import unicodedata
from collections import Counter

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CHARACTER_TOKEN_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI")


def tokens(text):
    normalized = unicodedata.normalize("NFC", str(text or "")).casefold()
    out = []
    for word in _TOKEN_RE.findall(normalized):
        if any(unicodedata.name(char, "").startswith(_CHARACTER_TOKEN_SCRIPTS)
               for char in word):
            out.extend(char for char in word if char.isalnum())
        else:
            out.append(word)
    return out


def ngrams(token_list, size):
    return list(zip(*(token_list[offset:] for offset in range(size))))


def counter_recall(source_items, output_items):
    if not source_items:
        return 1.0
    source = Counter(source_items)
    output = Counter(output_items)
    return sum(min(count, output.get(item, 0))
               for item, count in source.items()) / sum(source.values())
