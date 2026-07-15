"""Read-only, deterministic checks for annotated audiobook scripts."""

import re
from collections import Counter


_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_FRONT_MATTER_RE = re.compile(
    r"\b(?:copyright|all rights reserved|isbn(?:-1[03])?|table of contents)\b",
    re.IGNORECASE,
)
_NARRATION_RE = re.compile(
    r"\b(?:he|she|they|subaru|emilia)\s+(?:said|asked|replied|thought|looked|felt|was|were)\b",
    re.IGNORECASE,
)


def is_possible_misattributed_narration(text, speaker):
    return (bool(speaker) and speaker.casefold() not in {"narrator", "narration"}
            and bool(_NARRATION_RE.search(text)))


def _normalize(value):
    return " ".join(str(value or "").split()).casefold()


def _normalize_words(value):
    return " ".join(_WORD_RE.findall(_normalize(value)))


def _finding(severity, code, message, entry_numbers=None, **details):
    result = {"severity": severity, "code": code, "message": message}
    if entry_numbers:
        result["entry_numbers"] = entry_numbers
    if details:
        result["details"] = details
    return result


def find_adjacent_duplicate_blocks(texts, source_text):
    findings = []
    occupied = set()
    source_normalized = _normalize_words(source_text)
    for block_size in range(5, 1, -1):
        index = 0
        while index + (2 * block_size) <= len(texts):
            left = texts[index:index + block_size]
            right = texts[index + block_size:index + (2 * block_size)]
            positions = set(range(index, index + (2 * block_size)))
            if (positions.isdisjoint(occupied) and left == right and
                    all(len(text) >= 8 for text in left)):
                block_text = _normalize_words(" ".join(left))
                source_occurrences = source_normalized.count(block_text) if source_normalized else None
                findings.append(_finding(
                    "blocking", "adjacent_duplicate_block",
                    f"Entries {index + 1}-{index + block_size} are repeated immediately.",
                    list(range(index + 1, index + (2 * block_size) + 1)),
                    block_size=block_size,
                    source_occurrences=source_occurrences,
                ))
                occupied.update(positions)
                index += 2 * block_size
            else:
                index += 1
    return findings


def audit_script(entries, source_text=None, is_generic_speaker_fn=None):
    """Return deterministic findings without modifying ``entries``."""
    findings = []
    if not isinstance(entries, list):
        findings.append(_finding("blocking", "invalid_script", "The script root must be a JSON array."))
        return _build_report(0, findings)

    texts = []
    instructions = []
    valid_entries = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            findings.append(_finding("blocking", "invalid_entry", "Entry must be a JSON object.", [index]))
            texts.append("")
            continue

        valid_entries.append((index, entry))
        text = str(entry.get("text") or "").strip()
        speaker = str(entry.get("speaker") or "").strip()
        instruct = str(entry.get("instruct") or "").strip()
        texts.append(_normalize(text))
        instructions.append(_normalize(instruct))

        if not text:
            findings.append(_finding("blocking", "empty_text", "Entry has no speakable text.", [index]))
        if not speaker:
            findings.append(_finding("blocking", "missing_speaker", "Entry has no speaker.", [index]))
        if not instruct:
            findings.append(_finding("manual_review", "missing_instruction", "Entry has no delivery instruction.", [index]))
        cyrillic = sorted(set(_CYRILLIC_RE.findall(text)))
        if cyrillic:
            findings.append(_finding(
                "blocking", "cyrillic_in_text",
                "Entry contains Cyrillic characters that may be Latin homoglyphs.",
                [index], characters=cyrillic,
            ))
        if index <= 30 and _FRONT_MATTER_RE.search(text):
            findings.append(_finding("manual_review", "front_matter", "Possible publication front matter.", [index]))
        if is_possible_misattributed_narration(text, speaker):
            findings.append(_finding(
                "manual_review", "possible_misattributed_narration",
                "Third-person narration may be assigned to a character.", [index], speaker=speaker,
            ))
        if speaker and is_generic_speaker_fn and is_generic_speaker_fn(speaker):
            findings.append(_finding(
                "manual_review", "generic_speaker", "Generic speaker label needs book-local review.",
                [index], speaker=speaker,
            ))

    findings.extend(find_adjacent_duplicate_blocks(texts, source_text))

    nonempty_instructions = [value for value in instructions if value]
    if len(nonempty_instructions) >= 20:
        uniqueness = len(set(nonempty_instructions)) / len(nonempty_instructions)
        if uniqueness >= 0.95:
            findings.append(_finding(
                "informational", "high_instruction_uniqueness",
                "Nearly every entry has a unique delivery instruction.",
                uniqueness=round(uniqueness, 4),
            ))

    if source_text:
        source_words = Counter(_WORD_RE.findall(_normalize(source_text)))
        script_words = Counter(word for text in texts for word in _WORD_RE.findall(text))
        source_total = sum(source_words.values())
        matched = sum(min(count, script_words.get(word, 0)) for word, count in source_words.items())
        coverage = matched / source_total if source_total else 1.0
        if source_total and coverage < 0.85:
            findings.append(_finding(
                "manual_review", "low_source_word_coverage",
                "Annotated text has low word coverage relative to the source.",
                coverage=round(coverage, 4),
            ))

    return _build_report(len(entries), findings)


def _build_report(entry_count, findings):
    counts = {severity: 0 for severity in ("blocking", "manual_review", "informational")}
    for finding in findings:
        counts[finding["severity"]] += 1
    return {
        "entry_count": entry_count,
        "counts": counts,
        "can_apply_repairs": counts["blocking"] == 0,
        "findings": findings,
    }
