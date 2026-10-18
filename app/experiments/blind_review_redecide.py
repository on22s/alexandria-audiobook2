"""#1290 blind re-decide: hide speaker labels from labels-only review, then keep
the original label wherever the model answers UNKNOWN.

    blind_review_redecide.py prepare --script S0.json --output S3_blind.input.json
    blind_review_redecide.py merge --script S0.json --reviewed S3_blind.json \
        --output S3_blind_merged.json --summary S3_blind_merge.json

`prepare` replaces the label of every row that is spoken or carries a character
name with UNKNOWN; narration rows keep NARRATOR. Labels-only review keeps rows in
order and never edits text, so `merge` pairs rows by position and refuses when
the row count or any text differs. The summary holds counts only.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.provenance import provenance
from utils import atomic_json_write

HIDDEN = 'UNKNOWN'
NARRATOR = 'NARRATOR'


def is_blinded_row(entry):
    return entry.get('spoken') is True or entry.get('speaker') != NARRATOR


def is_unknown_label(speaker):
    return not isinstance(speaker, str) or speaker.strip().upper() in ('', HIDDEN)


def get_blind_script(entries):
    """Return a copy with every blinded row's speaker shown as UNKNOWN."""
    return [{**copy.deepcopy(entry), 'speaker': HIDDEN} if is_blinded_row(entry)
            else copy.deepcopy(entry) for entry in entries]


def get_redecided_script(original, reviewed):
    """Return (merged rows, counts): the reviewed label, or the original on UNKNOWN."""
    if len(original) != len(reviewed):
        raise ValueError('reviewed row count differs from the original')
    merged, counts = [], {'rows': len(original), 'blinded': 0, 'kept_on_unknown': 0,
                          'relabelled': 0, 'confirmed': 0, 'narration_changed': 0}
    for before, after in zip(original, reviewed):
        if before.get('text') != after.get('text'):
            raise ValueError('reviewed text differs from the original')
        row = copy.deepcopy(after)
        if not is_blinded_row(before):
            if after.get('speaker') != before.get('speaker'):
                counts['narration_changed'] += 1
        else:
            counts['blinded'] += 1
            if is_unknown_label(after.get('speaker')):
                row['speaker'] = before.get('speaker')
                counts['kept_on_unknown'] += 1
            elif after['speaker'] == before.get('speaker'):
                counts['confirmed'] += 1
            else:
                counts['relabelled'] += 1
        merged.append(row)
    return merged, counts


def read_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--script', required=True)
    prep.add_argument('--output', required=True)
    merge = sub.add_parser('merge')
    merge.add_argument('--script', required=True)
    merge.add_argument('--reviewed', required=True)
    merge.add_argument('--output', required=True)
    merge.add_argument('--summary', required=True)
    args = parser.parse_args(argv)
    original = read_json(args.script)
    if args.command == 'prepare':
        blind = get_blind_script(original)
        atomic_json_write(blind, args.output)
        print(f"blinded {sum(map(is_blinded_row, original))} of {len(original)} rows")
        return 0
    merged, counts = get_redecided_script(original, read_json(args.reviewed))
    atomic_json_write(merged, args.output)
    atomic_json_write({**counts, 'provenance': provenance(__file__, args)}, args.summary)
    print(json.dumps(counts))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
