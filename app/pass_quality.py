"""Validators for the three-pass generation flow (segment / attribute /
instruct). Segment reuses recall_core for source-fidelity scoring; attribute and
instruct enforce a hard per-entry text freeze (they may only add fields)."""

from recall_core import tokens, ngrams, counter_recall
from review_script import normalize_text

MIN_SOURCE_TOKEN_RECALL = 0.90
MIN_ORDERED_TRIGRAM_RECALL = 0.90
MIN_OUTPUT_SOURCE_RATIO = 0.90
MAX_OUTPUT_SOURCE_RATIO = 1.10
_VALID_SEGMENT_TYPES = {"NARRATOR", "SPOKEN"}


def validate_segment_quality(source_text, entries):
    """Fidelity gate for pass 1 output [{type, text}]. Same recall/trigram math
    as the single-pass gate, but validates the segment shape (type in
    {NARRATOR, SPOKEN}) rather than speaker/instruct."""
    findings = []
    if not isinstance(entries, list) or not entries:
        return _report(0, 0, 0.0, 0.0, 0.0,
                       [{"code": "missing_entries", "message": "No entries."}])
    output_parts = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number})
            continue
        missing = [k for k in ("type", "text") if k not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing})
        if entry.get("type") not in _VALID_SEGMENT_TYPES:
            findings.append({"code": "invalid_type", "entry_number": number,
                             "value": entry.get("type")})
        text = str(entry.get("text") or "")
        if not text.strip():
            findings.append({"code": "empty_text", "entry_number": number})
        output_parts.append(text)

    source_tokens = tokens(source_text)
    output_tokens = tokens(" ".join(output_parts))
    sc, oc = len(source_tokens), len(output_tokens)
    recall = counter_recall(source_tokens, output_tokens)
    trigram = counter_recall(ngrams(source_tokens, 3), ngrams(output_tokens, 3))
    ratio = oc / sc if sc else (1.0 if not oc else 0.0)
    if sc and recall < MIN_SOURCE_TOKEN_RECALL:
        findings.append({"code": "low_source_token_recall", "value": round(recall, 4)})
    if sc >= 3 and trigram < MIN_ORDERED_TRIGRAM_RECALL:
        findings.append({"code": "low_ordered_trigram_recall", "value": round(trigram, 4)})
    if sc and not MIN_OUTPUT_SOURCE_RATIO <= ratio <= MAX_OUTPUT_SOURCE_RATIO:
        findings.append({"code": "output_source_ratio", "value": round(ratio, 4)})
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


def freeze_check(frozen_entries, new_entries):
    """Return (ok, reason). ok iff new_entries has the same count as
    frozen_entries and each new entry's text matches the frozen text under
    normalize_text (case/punctuation/whitespace-insensitive, same comparison
    review uses). new_entries may add fields; it may not change or reorder text."""
    if len(new_entries) != len(frozen_entries):
        return False, f"count {len(new_entries)} != frozen {len(frozen_entries)}"
    for i, (frozen, new) in enumerate(zip(frozen_entries, new_entries), 1):
        if normalize_text(new.get("text", "")) != normalize_text(frozen.get("text", "")):
            return False, f"entry {i} text changed"
    return True, ""


def validate_attribution(frozen_entries, named_entries):
    """Pass 2 gate. Enforces the freeze, then requires every SPOKEN span to have
    a non-empty speaker other than NARRATOR, and every NARRATOR span to stay
    NARRATOR."""
    findings = []
    ok, reason = freeze_check(frozen_entries, named_entries)
    if not ok:
        findings.append({"code": "text_freeze_violated", "message": reason})
        return {"passed": False, "findings": findings}
    for i, (frozen, named) in enumerate(zip(frozen_entries, named_entries), 1):
        speaker = (named.get("speaker") or "").strip()
        if frozen["type"] == "SPOKEN":
            if not speaker or speaker.upper() == "NARRATOR":
                findings.append({"code": "spoken_not_named", "entry_number": i})
        else:  # NARRATOR
            if speaker.upper() != "NARRATOR":
                findings.append({"code": "narrator_renamed", "entry_number": i,
                                 "value": speaker})
    return {"passed": not findings, "findings": findings}


def validate_instruct(prior_entries, annotated_entries):
    """Pass 3 gate. Enforces the freeze on text AND speaker (pass 3 may only add
    instruct), and requires a non-empty instruct on every entry."""
    findings = []
    ok, reason = freeze_check(prior_entries, annotated_entries)
    if not ok:
        findings.append({"code": "text_freeze_violated", "message": reason})
        return {"passed": False, "findings": findings}
    for i, (prior, ann) in enumerate(zip(prior_entries, annotated_entries), 1):
        if (ann.get("speaker") or "") != (prior.get("speaker") or ""):
            findings.append({"code": "speaker_changed", "entry_number": i})
        if not (ann.get("instruct") or "").strip():
            findings.append({"code": "missing_instruct", "entry_number": i})
    return {"passed": not findings, "findings": findings}
