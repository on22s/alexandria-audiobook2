"""Bounded source-quote validation shared by roster review and offline evaluation.

Offsets count Unicode characters in the exact supplied text. A caller preparing
text must bind its preparation settings and fingerprint before using this API.
A quote locates words; it never establishes the speaker's identity.
"""
import hashlib
import re

from cast_list import MAX_SOURCE_CHARS

MAX_ENTRIES = 200
MAX_EVIDENCE = 8
MAX_QUOTE_CHARS = 4000


def _require_keys(value, keys, label):
    if not isinstance(value, dict) or len(value) != len(keys) or set(value) != set(keys):
        raise ValueError(f'{label} has an invalid structure')


def _require_text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or not value.strip():
        raise ValueError(f'{label} must be nonempty text within {limit} characters')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'{label} must be valid UTF-8 text') from None


def validate_source_evidence_packet(source_characters, source_sha256, packet):
    """Validate structure/ranges without scanning a source or approving identity.

    Historical rosters can be checked against their recorded source coordinates;
    callers must still bind those coordinates to a trusted source snapshot.
    """
    if type(source_characters) is not int or not 1 <= source_characters <= MAX_SOURCE_CHARS:
        raise ValueError('Source character count is out of bounds')
    if not isinstance(source_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', source_sha256) is None:
        raise ValueError('Source fingerprint must be a lowercase SHA-256 digest')
    _require_keys(packet, ('source_sha256', 'entries'), 'Packet')
    if packet['source_sha256'] != source_sha256:
        raise ValueError('Source fingerprint differs; prepare a new review for this revision')
    entries = packet['entries']
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError('Packet requires between 1 and 200 entries')
    seen = set()
    # Validate every claim before searching, so malformed imports never produce
    # a plausible partial result or trigger unnecessary source scans.
    for entry in entries:
        _require_keys(entry, ('id', 'canonical_name', 'evidence'), 'Entry')
        if (not isinstance(entry['id'], str)
                or re.fullmatch(r'character_[0-9a-f]{20}', entry['id']) is None
                or entry['id'] in seen):
            raise ValueError('Entry IDs must be unique opaque character IDs')
        seen.add(entry['id'])
        _require_text(entry['canonical_name'], 1024, 'Canonical name')
        evidence = entry['evidence']
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
            raise ValueError('Each entry requires between 1 and 8 evidence claims')
        for claim in evidence:
            _require_keys(claim, ('source_quote', 'start_char', 'end_char'), 'Evidence')
            quote, start, end = (claim[key] for key in ('source_quote', 'start_char', 'end_char'))
            _require_text(quote, MAX_QUOTE_CHARS, 'Evidence quote')
            if start is None and end is None:
                continue
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= source_characters:
                raise ValueError('Evidence requires two null offsets or a valid source character range')


def get_source_evidence_review(source_text, packet):
    """Validate all claims first, then return a detached quote-location report."""
    _require_text(source_text, MAX_SOURCE_CHARS, 'Source')
    fingerprint = hashlib.sha256(source_text.encode('utf-8')).hexdigest()
    validate_source_evidence_packet(len(source_text), fingerprint, packet)
    entries = packet['entries']
    rows = []
    counts = {key: 0 for key in ('exact', 'unique_candidate', 'ambiguous', 'unsupported', 'offset_mismatch')}
    for entry in entries:
        evidence = []
        for claim in entry['evidence']:
            quote, start, end = (claim[key] for key in ('source_quote', 'start_char', 'end_char'))
            candidates = []
            if start is not None and source_text[start:end] == quote:
                status = 'exact'
            else:
                position = source_text.find(quote)
                # Two occurrences suffice to establish ambiguity. Include
                # overlapping matches, and never silently relocate bad offsets.
                while position >= 0 and len(candidates) < 2:
                    candidates.append({'start_char': position, 'end_char': position + len(quote)})
                    if len(candidates) == 2:
                        break
                    position = source_text.find(quote, position + 1)
                status = ('unsupported' if not candidates else
                          'offset_mismatch' if start is not None else
                          'unique_candidate' if len(candidates) == 1 else 'ambiguous')
            counts[status] += 1
            evidence.append({**claim, 'status': status, 'candidate_locations': candidates})
        rows.append({'id': entry['id'], 'canonical_name': entry['canonical_name'],
                     'identity_status': 'not_evaluated', 'evidence': evidence})
    return {'schema': 1, 'source_sha256': fingerprint, 'source_characters': len(source_text),
            'counts': counts, 'entries': rows,
            'qualification': 'Quote locations only; character identity and aliases are not approved. '
                             'Candidate locations require explicit review; source changes require a new packet.'}
