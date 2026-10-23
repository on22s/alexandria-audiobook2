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
import json
from pathlib import Path

from adapter_publication import save_adapter_publication_bytes
from cast_list import MAX_SOURCE_CHARS
from experiments.provenance import input_sha256, provenance
from project_scope_files import capture_scope_file

# Re-export the evaluator API and limits for existing offline callers.
from source_evidence import (MAX_ENTRIES, MAX_EVIDENCE, MAX_QUOTE_CHARS,
                             get_source_evidence_review)

MAX_PACKET_BYTES = 2 * 1024**2
MAX_SOURCE_BYTES = 4 * MAX_SOURCE_CHARS


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
                      inputs={'source': source_receipt, 'packet': packet_receipt},
                      implementation=input_sha256([Path(__file__).resolve().parents[1] / 'source_evidence.py']))
        encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
        out.mkdir(mode=0o700, exist_ok=False)
        save_adapter_publication_bytes(str(out / 'report.json'), encoded)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        parser.error(str(error))
    print(json.dumps({'status': 'evaluated', 'counts': report['counts'], 'out': str(out)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
