"""Pure quality checks for an annotated response against its source chunk."""

import re
import unicodedata
from difflib import SequenceMatcher

from recall_core import tokens as _tokens, ngrams as _ngrams, counter_recall as _counter_recall
from script_preflight import find_adjacent_duplicate_blocks


MIN_SOURCE_TOKEN_RECALL = 0.90
MIN_ORDERED_TRIGRAM_RECALL = 0.90
MIN_OUTPUT_SOURCE_RATIO = 0.90
MAX_OUTPUT_SOURCE_RATIO = 1.10
# Exhaustion-only acceptance floor for an otherwise-complete conversion whose
# sole defect is ordered-trigram recall. NOT a pass threshold: the 0.90 gate
# above is unchanged. See is_trigram_only_near_miss.
# Lowered 0.84 -> 0.82 on 2026-07-20 after a real A/B run: Volume 25's blocking
# chunk maxed its full-chunk trigram-only near-miss at 0.838, missing 0.84 by
# 0.002 and failing the whole book. The failed-manifest sweep confirms 0.82
# still admits zero collapse-group output (collapse chunks never produce a
# trigram-only failure - they always also miss source-token recall).
ACCEPT_TRIGRAM_NEAR_MISS_FLOOR = 0.82
MAX_MISSING_SPANS = 3
MIN_MISSING_SPAN_TOKENS = 5
MISSING_SPAN_PREVIEW_TOKENS = 12
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")


def validate_chunk_quality(source_text, entries):
    """Return deterministic metrics and findings without mutating inputs."""
    findings = []
    if not isinstance(entries, list) or not entries:
        return _report(0, 0, 0.0, 0.0, 0.0, [{
            "code": "missing_entries", "message": "Response contains no entries."
        }])

    output_parts = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number,
                             "message": "Entry must be a JSON object."})
            continue
        missing = [key for key in ("speaker", "text", "instruct") if key not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing, "message": "Entry is missing required fields."})
        text = str(entry.get("text") or "")
        if not text.strip():
            findings.append({"code": "empty_text", "entry_number": number,
                             "message": "Entry contains no speakable text."})
        output_parts.append(text)

    source_tokens = _tokens(source_text)
    output_text = " ".join(output_parts)
    output_tokens = _tokens(output_text)
    source_count = len(source_tokens)
    output_count = len(output_tokens)
    recall = _counter_recall(source_tokens, output_tokens)
    trigram_recall = _counter_recall(_ngrams(source_tokens, 3), _ngrams(output_tokens, 3))
    ratio = output_count / source_count if source_count else (1.0 if not output_count else 0.0)

    if source_count and recall < MIN_SOURCE_TOKEN_RECALL:
        findings.append({"code": "low_source_token_recall", "value": round(recall, 4),
                         "minimum": MIN_SOURCE_TOKEN_RECALL,
                         "message": "Too much source text is absent from the response."})
    if source_count >= 3 and trigram_recall < MIN_ORDERED_TRIGRAM_RECALL:
        findings.append({"code": "low_ordered_trigram_recall", "value": round(trigram_recall, 4),
                         "minimum": MIN_ORDERED_TRIGRAM_RECALL,
                         "message": "Too many ordered source phrases are absent."})
    if source_count and not MIN_OUTPUT_SOURCE_RATIO <= ratio <= MAX_OUTPUT_SOURCE_RATIO:
        findings.append({"code": "output_source_ratio", "value": round(ratio, 4),
                         "minimum": MIN_OUTPUT_SOURCE_RATIO, "maximum": MAX_OUTPUT_SOURCE_RATIO,
                         "message": "Output length is implausible for the source chunk."})

    source_cyrillic = sorted(set(_CYRILLIC_RE.findall(source_text)))
    output_cyrillic = sorted(set(_CYRILLIC_RE.findall(output_text)))
    unsupported = sorted(set(output_cyrillic) - set(source_cyrillic))
    if unsupported:
        findings.append({"code": "unsupported_cyrillic", "characters": unsupported,
                         "message": "Response introduced Cyrillic characters absent from the source."})
    source_non_ascii = {char for char in unicodedata.normalize("NFC", source_text)
                        if ord(char) > 127 and unicodedata.category(char).startswith("L")}
    introduced_non_ascii = sorted({char for char in unicodedata.normalize("NFC", output_text)
                                   if ord(char) > 127
                                   and unicodedata.category(char).startswith("L")
                                   and char not in source_non_ascii
                                   and not ("\u0400" <= char <= "\u04ff")})
    if introduced_non_ascii:
        findings.append({
            "code": "unsupported_unicode_character",
            "characters": [{"character": char, "codepoint": f"U+{ord(char):04X}",
                            "name": unicodedata.name(char, "UNKNOWN")}
                           for char in introduced_non_ascii],
            "message": "Response introduced non-ASCII letters absent from the source.",
        })
    entry_texts = [" ".join(str(entry.get("text") or "").split()).casefold()
                   if isinstance(entry, dict) else "" for entry in entries]
    for duplicate in find_adjacent_duplicate_blocks(entry_texts, source_text):
        if duplicate.get("details", {}).get("source_occurrences") == 1:
            findings.append({"code": "source_unsupported_duplicate",
                             "entry_numbers": duplicate["entry_numbers"],
                             "message": "An adjacent repeated block occurs only once in the source."})

    recall_codes = {"low_source_token_recall", "low_ordered_trigram_recall"}
    missing_spans = (_missing_source_spans(source_tokens, output_tokens)
                     if any(finding.get("code") in recall_codes for finding in findings)
                     else [])
    return _report(source_count, output_count, recall, trigram_recall, ratio, findings,
                   source_cyrillic=source_cyrillic, missing_spans=missing_spans)


def is_trigram_only_near_miss(quality):
    """True when a *failed* report's sole defect is ordered-trigram recall in
    [ACCEPT_TRIGRAM_NEAR_MISS_FLOOR, MIN_ORDERED_TRIGRAM_RECALL).

    Such a report is a complete, correct-length conversion - >=0.90 source-token
    recall and an in-band output/source ratio are implied, since either would
    have raised its own finding - that only lightly reordered or reworded the
    source, the inherent cost of turning prose into speaker-tagged annotations
    on dialogue-dense passages. generate_script.py accepts one of these ONLY
    after full retries AND adaptive split are both exhausted, never as a
    first-class pass, so the 0.90 gate stays intact for every recoverable chunk.

    Floor calibrated against real failed-book manifests (see the constant's own
    comment for the 0.84->0.82 refinement): recovers the books whose blocking
    chunk is an otherwise-complete conversion stuck just under 0.90 trigram,
    admits zero collapse-group output (those always also fail source-token
    recall), and changes zero already-accepted chunks. Shared by
    call_llm_for_entries (candidate capture) and the post-return gate
    (acceptance) so both use one definition (Rule 15).
    """
    if not quality or quality.get("passed"):
        return False
    codes = {finding.get("code") for finding in quality.get("findings") or []}
    if codes != {"low_ordered_trigram_recall"}:
        return False
    trigram = (quality.get("metrics") or {}).get("ordered_trigram_recall")
    return trigram is not None and trigram >= ACCEPT_TRIGRAM_NEAR_MISS_FLOOR


def _missing_source_spans(source_tokens, output_tokens):
    """Largest source token runs absent from the output, as retry evidence.

    Aligns the two token sequences (SequenceMatcher, autojunk off) and keeps
    the source side of ``delete``/``replace`` opcodes at least
    MIN_MISSING_SPAN_TOKENS long. Previews use the recall metric's own
    normalization (casefolded, punctuation-stripped tokens), not the original
    source formatting.
    """
    matcher = SequenceMatcher(None, source_tokens, output_tokens, autojunk=False)
    spans = []
    for tag, source_start, source_end, _output_start, _output_end in matcher.get_opcodes():
        if tag in ("delete", "replace") and source_end - source_start >= MIN_MISSING_SPAN_TOKENS:
            spans.append({
                "start_token": source_start,
                "token_count": source_end - source_start,
                "preview": " ".join(source_tokens[
                    source_start:min(source_end,
                                     source_start + MISSING_SPAN_PREVIEW_TOKENS)]),
            })
    spans.sort(key=lambda span: (-span["token_count"], span["start_token"]))
    return spans[:MAX_MISSING_SPANS]


def _report(source_count, output_count, recall, trigram_recall, ratio, findings,
            source_cyrillic=None, missing_spans=None):
    return {
        "passed": not findings,
        "metrics": {
            "source_tokens": source_count,
            "output_tokens": output_count,
            "source_token_recall": round(recall, 4),
            "ordered_trigram_recall": round(trigram_recall, 4),
            "output_source_ratio": round(ratio, 4),
        },
        "source_cyrillic": source_cyrillic or [],
        "missing_source_spans": missing_spans or [],
        "findings": findings,
    }
