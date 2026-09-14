"""Where the generated script diverges from the source (issue #522 s7.4/7.5).

The text-integrity checks report *how much* text was lost or gained; this
says *where*. Both sides are reduced to the words `review_script.normalize_text`
uses (lowercase, punctuation stripped), so quote marks, italics and the
chunker's whitespace never show up as differences - only words do.
"""
import bisect
import difflib

from generate_script import split_into_chunk_records
from review_script import normalize_text

CONTEXT_WORDS = 3


def _words_with_owners(entries):
    """-> (words, owner) where owner[i] is the index of the entry word i came from."""
    words, owner = [], []
    for index, entry in enumerate(entries):
        for w in normalize_text(entry.get("text") or "").split():
            words.append(w)
            owner.append(index)
    return words, owner


def _source_chunk_starts(source_text):
    """Word offset at which each chunk of the source begins, for hunk labels."""
    starts, total = [], 0
    for record in split_into_chunk_records(source_text):
        starts.append(total)
        total += len(normalize_text(record["text"]).split())
    return starts


def word_diff(source_text, entries, context=CONTEXT_WORDS):
    """-> {"hunks": [...], "totals": {...}}: one hunk per differing region."""
    source_words = normalize_text(source_text).split()
    script_words, owner = _words_with_owners(entries)
    starts = _source_chunk_starts(source_text)
    matcher = difflib.SequenceMatcher(a=source_words, b=script_words, autojunk=False)
    hunks = []
    totals = {"source_words": len(source_words), "script_words": len(script_words),
              "deleted": 0, "inserted": 0, "replaced": 0}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        kind = {"delete": "deleted", "insert": "inserted", "replace": "replaced"}[tag]
        totals[kind] += max(i2 - i1, j2 - j1)
        hunks.append({
            "kind": tag,
            "source_pos": i1,
            "chunk": bisect.bisect_right(starts, i1) if starts else 1,
            "entry_index": owner[j1] if j1 < len(owner) else (owner[j1 - 1] if owner and j1 else None),
            "source_before": " ".join(source_words[max(0, i1 - context):i1]),
            "source_words": " ".join(source_words[i1:i2]),
            "source_after": " ".join(source_words[i2:i2 + context]),
            "script_words": " ".join(script_words[j1:j2]),
        })
    return {"hunks": hunks, "totals": totals, "chunks_total": len(starts)}
