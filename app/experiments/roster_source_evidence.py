"""Offline source-quote evaluation for #1443; never approves character identity.

Run from app/: python -m experiments.roster_source_evidence --source book.txt
    --packet claims.json --out-dir /outside/checkout/new-evaluation

The evaluation packet is deliberately smaller than a donor roster: exactly
source_sha256 and entries, each with id, canonical_name and evidence. Evidence
contains source_quote, start_char and end_char (both offsets may be null).
Offsets count Python Unicode characters in exact UTF-8 text, without newline
or quote normalization. A unique unanchored match is only a review candidate.
Absent quotes are unsupported even when offsets were supplied; offset mismatch
means the quote exists elsewhere. Unpaired Unicode surrogates are refused.
Even an exact quote proves its location, not the claimed person's identity.
Approvals, alias changes, imports and rollback remain outside this evaluator.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from adapter_publication import save_adapter_publication_bytes
from cast_list import MAX_SOURCE_CHARS
from experiments.provenance import provenance
from project_scope_files import capture_scope_file

MAX_ENTRIES = 200
MAX_EVIDENCE = 8
MAX_QUOTE_CHARS = 4000
MAX_PACKET_BYTES = 2 * 1024**2
MAX_SOURCE_BYTES = 4 * MAX_SOURCE_CHARS


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


def get_source_evidence_review(source_text, packet):
    """Validate an evaluation packet, then return a detached, bounded report."""
    _require_text(source_text, MAX_SOURCE_CHARS, 'Source')
    _require_keys(packet, ('source_sha256', 'entries'), 'Packet')
    fingerprint = hashlib.sha256(source_text.encode('utf-8')).hexdigest()
    if packet['source_sha256'] != fingerprint:
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
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source_text):
                raise ValueError('Evidence requires two null offsets or a valid source character range')
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


def _read_input(path, limit):
    path = Path(path).absolute()
    receipt, raw = capture_scope_file(path.parent, path.name, limit=limit, include_bytes=True)
    if raw is None:
        raise ValueError('A selected evaluation input is missing')
    return receipt, raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--packet', required=True)
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args(argv)
    try:
        source_receipt, source = _read_input(args.source, MAX_SOURCE_BYTES)
        packet_receipt, packet = _read_input(args.packet, MAX_PACKET_BYTES)
        report = get_source_evidence_review(source.decode('utf-8'), json.loads(packet))
        root = Path(__file__).resolve().parents[2]
        out = Path(args.out_dir).absolute()
        if out == root or root in out.parents or out.resolve() != out:
            raise ValueError('Choose a new output directory outside the source checkout without path aliases')
        report.update(provenance=provenance(__file__, args),
                      inputs={'source': source_receipt, 'packet': packet_receipt})
        encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
        out.mkdir(mode=0o700, exist_ok=False)
        save_adapter_publication_bytes(str(out / 'report.json'), encoded)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        parser.error(str(error))
    print(json.dumps({'status': 'evaluated', 'counts': report['counts'], 'out': str(out)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
