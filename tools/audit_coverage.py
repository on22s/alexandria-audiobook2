#!/usr/bin/env python3
"""Initialize and validate coverage of the original codebase-review audit."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / 'AUDIT_COVERAGE.tsv'
SOURCE_SHA256 = 'a822366d76c1b6cd19a4f7da28a71a464574985a5bb0b3c01a1683991337e167'
ORIGINAL_ROWS_SHA256 = '29f33d873a4fc3bfeec5fdd32bbdddd373ed5f4aa6137b1005ea04ea3e70d6af'
FIELDS = ('id', 'group', 'file', 'line', 'angle', 'original_verdict',
          'summary', 'disposition', 'root_id', 'evidence')
CLOSED = {'fixed', 'already_fixed', 'duplicate', 'refuted', 'cleanup_done'}
DISPOSITIONS = CLOSED | {'unreviewed', 'open'}
FIXED_IDS = {21, 23, 24, 25, 26, 27, 28, 29, 30, 40, 41, 48, 72, 967,
             78, 79, 80, 81, 82, 83, 84, 85, 87, 88, 385, 386}


def initialize(source: Path):
    if LEDGER.exists():
        raise SystemExit(f'{LEDGER} already exists; refusing to overwrite dispositions')
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise SystemExit('Original audit SHA-256 does not match')
    candidates = json.loads(source.read_text())['candidates']
    if len(candidates) != 1374:
        raise SystemExit(f'Expected 1374 candidates, got {len(candidates)}')
    with LEDGER.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter='\t',
                                lineterminator='\n')
        writer.writeheader()
        for candidate_id, candidate in enumerate(candidates, 1):
            disposition = ('fixed' if candidate_id in FIXED_IDS else
                           'open' if candidate_id == 86 else 'unreviewed')
            writer.writerow({
                'id': candidate_id,
                'group': candidate['group'],
                'file': candidate['file'],
                'line': candidate['line'],
                'angle': candidate['angle'],
                'original_verdict': candidate['verdict'],
                'summary': candidate['summary'],
                'disposition': disposition,
                'root_id': '',
                'evidence': 'AUDIT_REMEDIATION.md' if disposition == 'fixed' else '',
            })


def check(require_closed: bool):
    with LEDGER.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise SystemExit('Coverage ledger columns do not match the schema')
        rows = list(reader)
    ids = [int(row['id']) for row in rows]
    if ids != list(range(1, 1375)):
        raise SystemExit('Coverage ledger must contain exactly IDs 1–1374 in order')
    original_rows = [[row[field] for field in FIELDS[:7]] for row in rows]
    original_bytes = json.dumps(original_rows, ensure_ascii=False,
                                separators=(',', ':')).encode()
    if hashlib.sha256(original_bytes).hexdigest() != ORIGINAL_ROWS_SHA256:
        raise SystemExit('Original candidate columns have changed')
    counts = Counter(row['disposition'] for row in rows)
    for row in rows:
        candidate_id = row['id']
        disposition = row['disposition']
        if disposition not in DISPOSITIONS:
            raise SystemExit(f'ID {candidate_id}: invalid disposition {disposition!r}')
        if disposition in CLOSED and not row['evidence'].strip():
            raise SystemExit(f'ID {candidate_id}: closed disposition needs evidence')
        if disposition == 'duplicate':
            try:
                root_id = int(row['root_id'])
            except ValueError:
                raise SystemExit(f'ID {candidate_id}: duplicate needs a root ID') from None
            if root_id == int(candidate_id) or not 1 <= root_id <= 1374:
                raise SystemExit(f'ID {candidate_id}: invalid duplicate root ID')
            if rows[root_id - 1]['disposition'] not in CLOSED - {'duplicate'}:
                raise SystemExit(f'ID {candidate_id}: duplicate root must be closed')
    if require_closed and (counts['unreviewed'] or counts['open']):
        raise SystemExit(f'PR gate failed: {counts["unreviewed"]} unreviewed, '
                         f'{counts["open"]} open')
    print(f'{len(rows)} IDs accounted for: ' + ', '.join(
        f'{name}={counts[name]}' for name in sorted(counts)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'check'))
    parser.add_argument('--source', type=Path, help='Original audit JSON (init only)')
    parser.add_argument('--require-closed', action='store_true')
    args = parser.parse_args()
    if args.action == 'init':
        if args.source is None:
            parser.error('init requires --source')
        initialize(args.source)
    check(args.require_closed)


if __name__ == '__main__':
    main()
