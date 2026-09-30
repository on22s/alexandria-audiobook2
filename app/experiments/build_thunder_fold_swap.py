"""Reverse the frozen nine/19 PDNC book split using the captured window builder."""
import argparse
import csv
import gzip
import hashlib
import types
import json
from pathlib import Path
import random
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / 'experiments'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdnc', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    source = APP.parent / 'ab_test_runtime/evidence/thunder_campaign_20260929/build_window25_michel2_full_20260924.py.gz'
    raw = gzip.decompress(source.read_bytes())
    builder = types.ModuleType('captured_window_builder')
    builder.__file__ = str(source)
    exec(compile(raw, str(source), 'exec'), builder.__dict__)
    builder.PDNC = Path(args.pdnc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    all_books = {p.name for p in (builder.PDNC / 'data').iterdir()
                 if (p / 'quotation_info.csv').is_file()}
    train_books = builder.EVAL_BOOKS
    assert len(all_books) == 28 and train_books <= all_books
    evaluation_books = sorted(all_books - train_books)
    assert len(evaluation_books) == 19
    rows, hashes, coverage = [], {}, {}
    rng = random.Random(20260924)
    for book in sorted(train_books):
        folder = builder.PDNC / 'data' / book
        with (folder / 'quotation_info.csv').open(encoding='utf-8-sig') as handle:
            count = sum(1 for _ in csv.DictReader(handle))
        indexes = set(rng.sample(range(count), min(200, count)))
        entries, positions, roster, _ = builder.source_entries(book, indexes)
        starts = sorted({positions[i] // 25 * 25 for i in indexes if i in positions})
        examples = [example for start in starts
                    if (example := builder.make_example(book, entries, start, roster))]
        assert examples, book
        for example in examples:
            targets = json.loads(example['completion'])
            assert targets and [t['n'] for t in targets] == list(range(len(targets)))
            assert all(t['speaker'] for t in targets)
        rows.extend(examples)
        coverage[book] = len(examples)
        hashes[book] = {name: builder.sha256(folder / name) for name in
                        ('novel_text.txt', 'quotation_info.csv', 'character_info.csv')}
    assert {r['book'].removeprefix('pdnc_') for r in rows} == train_books
    data = out / 'train_windows.jsonl'
    data.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    manifest = {'train_books': sorted(train_books), 'evaluation_books': evaluation_books,
                'coverage': coverage, 'examples': len(rows), 'data_sha256': builder.sha256(data),
                'builder_sha256': hashlib.sha256(raw).hexdigest(), 'seed': 20260924,
                'source_sha256': hashes, 'quote_samples_per_book': 200,
                'prompt_contract': 'captured michel2_full window25 surround2000',
                'limitation': 'Books are held out; authors are not guaranteed disjoint.'}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
