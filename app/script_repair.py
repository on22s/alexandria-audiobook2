"""Deterministic, source-backed repairs for annotated scripts."""

import copy
import re

from script_preflight import find_adjacent_duplicate_blocks, _normalize, _normalize_words, get_source_phrase_occurrences, get_source_block_occurrences, audit_unicode_text
from source_normalization import KNOWN_SOURCE_CORRUPTIONS


_WORD_WITH_CYRILLIC_RE = re.compile(r"[^\W\d_]*[\u0400-\u04ff][^\W\d_]*", re.UNICODE)
_CYRILLIC_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "у": "y", "к": "k", "м": "m", "т": "t", "в": "b", "г": "r",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X",
    "У": "Y", "К": "K", "М": "M", "Т": "T", "В": "B", "Г": "R",
})
EXPLICIT_SILENCE_MS = 1000


def build_deterministic_repair(entries, source_text, merge_empty_into_pause=True):
    """Return repaired copies and evidence; never mutate the supplied entries.

    merge_empty_into_pause (default True, single-pass behavior): convert an empty
    entry into a pause_after on its previous surviving entry and drop it.
    Preserve an explicit pause duration; keep untransferable metadata unresolved.
    The three-pass
    segment stage passes False: an empty {type,text} unit there should be surfaced
    by the segment gate's empty_text finding, not silently merged into a
    possibly different-typed neighbor before the gate ever sees it (finding #7)."""
    repaired = copy.deepcopy(entries)
    changes = []
    notes = []
    unresolved = []
    source_words = set(re.findall(r"[^\W_]+", _normalize(source_text), re.UNICODE))
    preserve_source_cyrillic = "LATIN" not in audit_unicode_text(source_text)["scripts"]

    for index, entry in enumerate(repaired):
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            continue
        original = entry["text"]
        replacements = []
        for match in _WORD_WITH_CYRILLIC_RE.finditer(original):
            old_word = match.group(0)
            known_corruption = old_word.casefold() in KNOWN_SOURCE_CORRUPTIONS
            if old_word.casefold() in source_words and (
                    preserve_source_cyrillic or (not known_corruption and
                    all("\u0400" <= char <= "\u04ff" for char in old_word))):
                # Faithful source words are evidence, including mixed-language
                # prose; known OCR corruption still follows its repair rules.
                continue
            new_word = KNOWN_SOURCE_CORRUPTIONS.get(old_word.casefold())
            if new_word and old_word[:1].isupper():
                new_word = new_word.capitalize()
            if not new_word:
                new_word = old_word.translate(_CYRILLIC_HOMOGLYPHS)
            if any("\u0400" <= char <= "\u04ff" for char in new_word):
                unresolved.append({"entry_number": index + 1, "text": old_word,
                                   "reason": "unsupported_cyrillic_character"})
            elif not known_corruption and old_word.casefold() in source_words:
                unresolved.append({"entry_number": index + 1, "text": old_word,
                                   "candidate": new_word, "reason": "original_in_source"})
            elif (new_word.casefold() not in source_words and
                  not (known_corruption and old_word.casefold() in source_words)):
                unresolved.append({"entry_number": index + 1, "text": old_word,
                                   "candidate": new_word, "reason": "candidate_not_in_source"})
            else:
                replacements.append((match.start(), match.end(), old_word, new_word))
        if replacements:
            updated = original
            for start, end, _old, new in reversed(replacements):
                updated = updated[:start] + new + updated[end:]
            entry["text"] = updated
            changes.append({
                "type": "unicode_homoglyph", "entry_number": index + 1,
                "before": original, "after": updated,
            })

    texts = [_normalize(entry.get("text")) if isinstance(entry, dict) else "" for entry in repaired]
    duplicate_findings = find_adjacent_duplicate_blocks(texts, source_text)
    removals = set()
    handled = set()
    source_normalized = _normalize_words(source_text)
    for finding in duplicate_findings:
        details = finding["details"]
        occurrences = details.get("source_occurrences")
        block_size = details["block_size"]
        first = finding["entry_numbers"][0]
        start = first - 1
        if any(index in handled for index in range(start, start + block_size)):
            continue
        block = texts[start:start + block_size]
        unit_size = next(size for size in range(1, block_size + 1)
                         if block_size % size == 0 and
                         block == block[:size] * (block_size // size))
        unit = block[:unit_size]
        emitted_copies = 2 * block_size // unit_size
        while texts[start + emitted_copies * unit_size:
                    start + (emitted_copies + 1) * unit_size] == unit:
            emitted_copies += 1
        handled.update(range(start, start + emitted_copies * unit_size))
        if unit_size < block_size:
            occurrences = get_source_block_occurrences(source_normalized, unit)
        # Three cases, and the old `!= 1` test collapsed two opposite ones.
        #
        #   0  the block is nowhere in the source: the model invented the
        #      repetition, and nothing here can decide which copy to keep.
        #      Genuinely unresolved.
        #   1  the block appears once but was emitted twice: the model
        #      duplicated it, so deleting the second copy restores the source.
        #  >=2 the SOURCE ITSELF repeats the block, so emitting it twice is
        #      faithful transcription. Deleting a copy would corrupt the text
        #      the model was asked to reproduce exactly.
        #
        # grimgar03 opens with its title 8 times - the source carries it 49
        # times in all - and every one of 17 attempts was rejected for
        # correctly reproducing that. The book could not be generated at all.
        if occurrences is None or occurrences == 0:
            unresolved.append({"entry_numbers": finding["entry_numbers"],
                               "reason": "duplicate_not_in_source"})
            continue
        if occurrences >= 2:
            if emitted_copies > occurrences:
                if not (get_source_phrase_occurrences(source_normalized, _normalize_words(" ".join(unit)))
                        if unit else 0):
                    unresolved.append({"entry_numbers": finding["entry_numbers"],
                                       "reason": "duplicate_exceeds_source_evidence"})
                    continue
                removed = list(range(start + occurrences * unit_size,
                                     start + emitted_copies * unit_size))
                removals.update(removed)
                changes.append({
                    "type": "adjacent_duplicate_block",
                    "kept_entry_numbers": list(range(first, first + occurrences * unit_size)),
                    "removed_entry_numbers": [index + 1 for index in removed],
                    "source_occurrences": occurrences,
                })
                continue
            # A NOTE, NOT A CHANGE. Keeping the block still has to be visible -
            # a future reader must be able to tell a block that was checked and
            # kept from one that was never examined - but `changes` is not the
            # place for it. Callers treat a non-empty `changes` as "the entries
            # differ from what was passed in, write them back": the repair
            # endpoint backs up the script, rewrites it with byte-identical
            # content and reports "repaired", and the generator prints
            # "Applied 1 deterministic chunk repair(s)". Both claim work that
            # did not happen, on exactly the books this branch exists for.
            notes.append({
                "type": "adjacent_duplicate_block_kept",
                "entry_numbers": finding["entry_numbers"],
                "source_occurrences": occurrences,
                "note": "source repeats this block; emitting it twice is "
                        "faithful, so nothing is removed",
            })
            continue
        removed = list(range(start + unit_size, start + emitted_copies * unit_size))
        removals.update(removed)
        changes.append({
            "type": "adjacent_duplicate_block",
            "kept_entry_numbers": list(range(first, first + unit_size)),
            "removed_entry_numbers": [index + 1 for index in removed],
            "source_occurrences": 1,
        })
    empty_indexes = ([index for index, entry in enumerate(repaired)
                      if isinstance(entry, dict) and not str(entry.get("text") or "").strip()]
                     if merge_empty_into_pause else [])
    for index in empty_indexes:
        if index == 0:
            unresolved.append({"entry_number": 1, "reason": "empty_first_entry"})
            continue
        extra_fields = set(repaired[index]) - {"text", "speaker", "instruct", "pause_after"}
        if extra_fields:
            unresolved.append({"entry_number": index + 1, "reason": "empty_entry_metadata",
                               "fields": sorted(extra_fields)})
            continue
        pause_ms = repaired[index].get("pause_after")
        if pause_ms is None:
            pause_ms = EXPLICIT_SILENCE_MS
        if isinstance(pause_ms, bool) or not isinstance(pause_ms, int) or pause_ms < 0:
            unresolved.append({"entry_number": index + 1, "reason": "invalid_empty_pause"})
            continue
        previous_index = index - 1
        while previous_index in removals:
            previous_index -= 1
        if previous_index < 0:
            unresolved.append({"entry_number": index + 1, "reason": "empty_first_entry"})
            continue
        previous = repaired[previous_index]
        if not isinstance(previous, dict):
            unresolved.append({"entry_number": index + 1, "reason": "invalid_previous_entry"})
            continue
        if not str(previous.get("text") or "").strip():
            unresolved.append({"entry_number": index + 1, "reason": "previous_entry_not_spoken"})
            continue
        if previous.get("pause_after") is not None:
            unresolved.append({"entry_number": index + 1, "reason": "previous_pause_already_set"})
            continue
        previous["pause_after"] = pause_ms
        changes.append({
            "type": "empty_entry_to_pause", "removed_entry_number": index + 1,
            "pause_after_entry_number": previous_index + 1, "pause_ms": pause_ms,
        })
        removals.add(index)
    for index in sorted(removals, reverse=True):
        del repaired[index]

    return {"entries": repaired, "changes": changes, "notes": notes,
            "unresolved": unresolved}
