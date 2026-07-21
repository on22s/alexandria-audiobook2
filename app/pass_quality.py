"""Validators for the three-pass generation flow (segment / attribute /
instruct). Segment reuses recall_core for source-fidelity scoring; attribute and
instruct enforce a hard per-entry text freeze (they may only add fields)."""

import unicodedata

from recall_core import tokens, ngrams, counter_recall
from review_script import normalize_text
# Import the fidelity thresholds and the introduced-character regex from the
# single-pass gate so pass 1 tracks any recalibration there automatically
# (e.g. the 0.84->0.82 near-miss floor history) instead of drifting.
from chunk_quality import (MIN_SOURCE_TOKEN_RECALL, MIN_ORDERED_TRIGRAM_RECALL,
                           MIN_OUTPUT_SOURCE_RATIO, MAX_OUTPUT_SOURCE_RATIO,
                           _CYRILLIC_RE)
from script_preflight import find_adjacent_duplicate_blocks

_VALID_SEGMENT_TYPES = {"NARRATOR", "SPOKEN"}


def _introduced_character_findings(source_text, output_text, entries):
    """Mirror the single-pass gate's introduced-character + adjacent-duplicate
    checks so pass 1 has the same fidelity bar (unsupported_cyrillic,
    unsupported_unicode_character, source_unsupported_duplicate). Text-only, so
    it is shape-agnostic across {type,text} and {speaker,text,instruct}."""
    findings = []
    source_cyrillic = set(_CYRILLIC_RE.findall(source_text))
    unsupported = sorted(set(_CYRILLIC_RE.findall(output_text)) - source_cyrillic)
    if unsupported:
        findings.append({"code": "unsupported_cyrillic", "characters": unsupported,
                         "message": "Response introduced Cyrillic characters absent from the source."})
    source_non_ascii = {char for char in unicodedata.normalize("NFC", source_text)
                        if ord(char) > 127 and unicodedata.category(char).startswith("L")}
    introduced = sorted({char for char in unicodedata.normalize("NFC", output_text)
                         if ord(char) > 127 and unicodedata.category(char).startswith("L")
                         and char not in source_non_ascii
                         and not ("Ѐ" <= char <= "ӿ")})
    if introduced:
        findings.append({"code": "unsupported_unicode_character",
                         "characters": [{"character": char, "codepoint": f"U+{ord(char):04X}",
                                         "name": unicodedata.name(char, "UNKNOWN")}
                                        for char in introduced],
                         "message": "Response introduced non-ASCII letters absent from the source."})
    entry_texts = [" ".join(str(e.get("text") or "").split()).casefold()
                   if isinstance(e, dict) else "" for e in entries]
    for duplicate in find_adjacent_duplicate_blocks(entry_texts, source_text):
        if duplicate.get("details", {}).get("source_occurrences") == 1:
            findings.append({"code": "source_unsupported_duplicate",
                             "entry_numbers": duplicate["entry_numbers"],
                             "message": "An adjacent repeated block occurs only once in the source."})
    return findings


def validate_segment_quality(source_text, entries):
    """Fidelity gate for pass 1 output [{type, text}]. Same recall/trigram math
    as the single-pass gate, but validates the segment shape (type in
    {NARRATOR, SPOKEN}) rather than speaker/instruct."""
    findings = []
    if not isinstance(entries, list) or not entries:
        return _report(0, 0, 0.0, 0.0, 0.0,
                       [{"code": "missing_entries", "message": "Response contains no entries."}])
    output_parts = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number,
                             "message": "Entry must be a JSON object."})
            continue
        missing = [k for k in ("type", "text") if k not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing, "message": "Entry is missing required fields."})
        if entry.get("type") not in _VALID_SEGMENT_TYPES:
            findings.append({"code": "invalid_type", "entry_number": number,
                             "value": entry.get("type"),
                             "message": "Each entry's type must be NARRATOR or SPOKEN."})
        text = str(entry.get("text") or "")
        if not text.strip():
            findings.append({"code": "empty_text", "entry_number": number,
                             "message": "Entry contains no speakable text."})
        output_parts.append(text)

    source_tokens = tokens(source_text)
    output_text = " ".join(output_parts)
    output_tokens = tokens(output_text)
    sc, oc = len(source_tokens), len(output_tokens)
    recall = counter_recall(source_tokens, output_tokens)
    trigram = counter_recall(ngrams(source_tokens, 3), ngrams(output_tokens, 3))
    ratio = oc / sc if sc else (1.0 if not oc else 0.0)
    if sc and recall < MIN_SOURCE_TOKEN_RECALL:
        findings.append({"code": "low_source_token_recall", "value": round(recall, 4),
                         "minimum": MIN_SOURCE_TOKEN_RECALL,
                         "message": "Too much source text is absent from the response."})
    if sc >= 3 and trigram < MIN_ORDERED_TRIGRAM_RECALL:
        findings.append({"code": "low_ordered_trigram_recall", "value": round(trigram, 4),
                         "minimum": MIN_ORDERED_TRIGRAM_RECALL,
                         "message": "Too many ordered source phrases are absent."})
    if sc and not MIN_OUTPUT_SOURCE_RATIO <= ratio <= MAX_OUTPUT_SOURCE_RATIO:
        findings.append({"code": "output_source_ratio", "value": round(ratio, 4),
                         "minimum": MIN_OUTPUT_SOURCE_RATIO, "maximum": MAX_OUTPUT_SOURCE_RATIO,
                         "message": "Output length is implausible for the source chunk."})
    findings.extend(_introduced_character_findings(source_text, output_text, entries))
    return _report(sc, oc, recall, trigram, ratio, findings)


def _report(source_count, output_count, recall, trigram, ratio, findings):
    return {
        "passed": not findings,
        "metrics": {
            "source_tokens": source_count, "output_tokens": output_count,
            "source_token_recall": round(recall, 4),
            "ordered_trigram_recall": round(trigram, 4),
            "output_source_ratio": round(ratio, 4),
        },
        "findings": findings,
    }


def _leading_tokens(text):
    return normalize_text(str(text or "")).split()


def index_head_check(frozen_entries, response_entries):
    """Return (ok, reason, ordered) for the index+head-anchor contract used by
    passes 2 and 3.

    The model no longer re-emits each entry's full text (which weak models drift
    on, corrupting a line and failing the whole batch). Instead it echoes, per
    entry, an index `n` and a `head` — the entry's first few words. This checks
    that the response has exactly one object per frozen entry, that every index
    0..k-1 appears exactly once (catches drops / dupes / miscounts), and that
    each head is the exact leading token-sequence of the frozen line it claims
    (catches misalignment — the model answering about the wrong line). On success
    `ordered[i]` is the response object bound to frozen entry i, so the caller can
    take only its speaker/instruct and keep the frozen text byte-exact."""
    k = len(frozen_entries)
    if not isinstance(response_entries, list) or len(response_entries) != k:
        got = len(response_entries) if isinstance(response_entries, list) else "non-list"
        return False, f"expected {k} entries, got {got}", None
    by_index = {}
    for item in response_entries:
        if not isinstance(item, dict):
            return False, "entry is not a JSON object", None
        n = item.get("n")
        if isinstance(n, float) and n.is_integer():
            n = int(n)
        if isinstance(n, bool) or not isinstance(n, int) or not (0 <= n < k):
            return False, f"entry has a missing or out-of-range index n={n!r}", None
        if n in by_index:
            return False, f"duplicate index n={n}", None
        by_index[n] = item
    ordered = []
    for i, frozen in enumerate(frozen_entries):
        item = by_index[i]  # complete permutation guaranteed by the checks above
        frozen_tokens = _leading_tokens(frozen.get("text"))
        head_tokens = _leading_tokens(item.get("head"))
        # Punctuation-only lines normalize to no tokens; nothing to anchor on, so
        # accept. Otherwise the head must be the exact leading words of the line.
        minimum = min(3, len(frozen_tokens))
        if frozen_tokens and (len(head_tokens) < minimum
                              or frozen_tokens[:len(head_tokens)] != head_tokens):
            return False, f"entry {i + 1} head anchor does not match the line start", None
        if head_tokens:
            matches = sum(
                _leading_tokens(other.get("text"))[:len(head_tokens)] == head_tokens
                for other in frozen_entries)
            if matches > 1:
                return False, (f"entry {i + 1} head anchor is ambiguous across "
                               f"{matches} lines"), None
        ordered.append(item)
    return True, "", ordered


def validate_attribution(frozen_entries, response_entries):
    """Pass 2 gate. Verifies the index+head alignment, then requires every SPOKEN
    span to have a non-empty speaker other than NARRATOR, and every NARRATOR span
    to stay NARRATOR."""
    ok, reason, ordered = index_head_check(frozen_entries, response_entries)
    if not ok:
        return {"passed": False,
                "findings": [{"code": "alignment_violated", "message": reason}]}
    findings = []
    for i, (frozen, item) in enumerate(zip(frozen_entries, ordered), 1):
        raw_speaker = item.get("speaker")
        speaker = raw_speaker.strip() if isinstance(raw_speaker, str) else ""
        if frozen.get("type") == "SPOKEN":
            if not speaker or speaker.upper() == "NARRATOR":
                findings.append({"code": "spoken_not_named", "entry_number": i,
                                 "message": "A spoken line was not assigned a character name."})
        else:  # NARRATOR (or any non-SPOKEN)
            if speaker.upper() != "NARRATOR":
                findings.append({"code": "narrator_renamed", "entry_number": i,
                                 "value": speaker,
                                 "message": "A narrator line must keep the speaker NARRATOR."})
    return {"passed": not findings, "findings": findings}


def validate_instruct(prior_entries, response_entries):
    """Pass 3 gate. Verifies the index+head alignment (speaker/text are supplied
    by the frozen entry, not the model, so they cannot change), and requires a
    non-empty instruct on every entry."""
    ok, reason, ordered = index_head_check(prior_entries, response_entries)
    if not ok:
        return {"passed": False,
                "findings": [{"code": "alignment_violated", "message": reason}]}
    findings = []
    for i, item in enumerate(ordered, 1):
        raw_instruct = item.get("instruct")
        if not isinstance(raw_instruct, str) or not raw_instruct.strip():
            findings.append({"code": "missing_instruct", "entry_number": i,
                             "message": "Every entry needs a non-empty instruct."})
    return {"passed": not findings, "findings": findings}
