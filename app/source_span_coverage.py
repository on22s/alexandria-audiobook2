"""Deterministic source-span tags and coverage checks for generation experiments."""

import re
from bisect import bisect_right
from collections import Counter
from recall_core import tokens


_BOUNDARY_RE = re.compile(r"""(?<=[.!?])(?P<closing>["'”’»›)\]}]*)\s+|\n\s*\n+""")


def get_source_spans(source_text):
    """Return ordered, non-empty sentence/paragraph spans without changing text."""
    spans = []
    start = 0
    for match in _BOUNDARY_RE.finditer(source_text):
        end = match.start() + len(match.group("closing") or "")
        text = source_text[start:end].strip()
        if text:
            spans.append({"id": f"S{len(spans) + 1:03d}", "text": text})
        start = match.end()
    tail = source_text[start:].strip()
    if tail:
        spans.append({"id": f"S{len(spans) + 1:03d}", "text": tail})
    return spans


def format_tagged_source(spans):
    """Render spans for a prompt while keeping the original span records pure."""
    return "\n".join(
        f'[{span["id"]}] {str(span["text"]).replace(chr(10), " ").replace(chr(13), " ")}'
        for span in spans)


def get_span_coverage_findings(spans, entries):
    """Validate declarations against ordered source text, allowing split spans."""
    expected = {span["id"] for span in spans}
    declared = set()
    findings = []
    for number, entry in enumerate(entries or [], 1):
        ids = entry.get("source_span_ids") if isinstance(entry, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            findings.append({"code": "invalid_source_span_ids", "entry_number": number})
            continue
        repeated = sorted(item for item, count in Counter(ids).items() if count > 1)
        if repeated:
            findings.append({"code": "duplicate_source_span_ids", "entry_number": number,
                             "span_ids": repeated})
        declared.update(ids)
    unknown = sorted(declared - expected)
    missing = sorted(expected - declared)
    if unknown:
        findings.append({"code": "unknown_source_span_ids", "span_ids": unknown})
    if missing:
        findings.append({"code": "uncovered_source_spans", "span_ids": missing})
    source_tokens = []
    ranges = []
    for span in spans:
        start = len(source_tokens)
        source_tokens.extend(tokens(span["text"]))
        ranges.append((start, len(source_tokens), span["id"]))
    entry_tokens = []
    for number, entry in enumerate(entries or [], 1):
        text = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(text, str) or not text.strip():
            findings.append({"code": "invalid_source_span_text", "entry_number": number})
            entry_tokens.append(None)
        else:
            entry_tokens.append(tokens(text))
    if any(part is None for part in entry_tokens):
        return findings
    output_tokens = [token for part in entry_tokens for token in part]
    if output_tokens != source_tokens:
        findings.append({"code": "source_span_text_mismatch",
                         "source_token_count": len(source_tokens),
                         "output_token_count": len(output_tokens)})
        return findings
    empty_spans = [span_id for start, end, span_id in ranges if start == end]
    if empty_spans:
        findings.append({"code": "unverifiable_source_span_text", "span_ids": empty_spans})
        return findings
    # Exact ordered tokens give unambiguous offsets even for repeated sentences.
    ends = [end for _, end, _ in ranges]
    offset = 0
    for number, (entry, part) in enumerate(zip(entries or [], entry_tokens), 1):
        end = offset + len(part)
        actual = []
        index = bisect_right(ends, offset)
        while index < len(ranges) and ranges[index][0] < end:
            actual.append(ranges[index][2])
            index += 1
        ids = entry.get("source_span_ids")
        if isinstance(ids, list) and ids and all(isinstance(item, str) for item in ids):
            if set(ids) != set(actual):
                findings.append({"code": "misassigned_source_span_ids", "entry_number": number,
                                 "declared_span_ids": ids[:], "expected_span_ids": actual})
        offset = end
    return findings
