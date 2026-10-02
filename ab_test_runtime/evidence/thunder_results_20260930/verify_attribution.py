"""Recompute a frozen paired evaluation against private answer keys and inputs."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys


def get_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--result', required=True)
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--training-manifest', required=True)
    parser.add_argument('--remote-input-hashes', required=True)
    parser.add_argument('--remote-hashes', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo) / 'app'))
    from experiments.scoring import alias_groups, normalize, same_speaker
    from experiments.lora_serving_eval import SPECIAL, make_windows, norm
    from three_pass_generate import get_deterministic_named_entry
    from experiments.provenance import get_harness_sha256_at_commit
    from scipy.stats import binomtest
    import numpy as np

    # Known positives AND negatives must discriminate before scoring the run.
    controls = [('MR. SMITH', 'MR SMITH', [], True),
                ('SMITH', 'JONES', [], False), ('SMITH', None, [], False),
                ('李明', '李明', [], True), ('李明', '张伟', [], False),
                ('SMITH', 'BOB', [{'SMITH', 'BOB'}], True)]
    for expected, actual, aliases, answer in controls:
        assert same_speaker(expected, actual, aliases) is answer

    root = Path(args.inputs)
    doc = json.loads(Path(args.result).read_text())
    manifest = json.loads((root / 'manifest.json').read_text())
    evaluation = json.loads((root / 'evaluation_manifest.json').read_text())
    training = json.loads(Path(args.training_manifest).read_text())
    remote = json.loads(Path(args.remote_input_hashes).read_text())
    remote_hashes = json.loads(Path(args.remote_hashes).read_text())
    for record in evaluation['files']:
        assert get_sha256(root / record['path']) == record['sha256'] == remote[record['path']]
    data_hash = get_sha256(root / 'train_windows.jsonl')
    assert data_hash == manifest['data_sha256'] == training['data'][0]['sha256']
    assert data_hash == next(v['sha256'] for k, v in remote_hashes.items()
                             if k.endswith('/fold_swap/train_windows.jsonl'))
    train_rows = [json.loads(line) for line in (root / 'train_windows.jsonl').read_text().splitlines()]
    book_key = lambda name: name.removeprefix('pdnc_').lower()
    train_books = {book_key(row['book']) for row in train_rows}
    eval_books = {book_key(name) for name in evaluation['evaluation_books']}
    assert len(train_rows) == manifest['examples'] == 975
    assert len(train_books) == 9 and len(eval_books) == 19
    assert not train_books & eval_books
    assert train_books == {book_key(name) for name in manifest['train_books']}
    assert eval_books == {book_key(name) for name in manifest['evaluation_books']}
    source = doc['meta']['git']
    assert not source['dirty']
    assert source['harness_sha256'] == get_harness_sha256_at_commit(args.repo, source['commit'])
    assert doc['meta']['validation'] == 'ok' and doc['meta'].get('finished')

    gold_by_book, expected_ids = {}, set()
    for book, digest in doc['meta']['gold_files'].items():
        path = root / 'fixtures' / f'attribution_gold_{book}.json'
        assert get_sha256(path) == digest
        gold = json.loads(path.read_text())
        gold_by_book[book] = gold
        checkpoint = json.loads((root / 'checkpoints' /
            f'{book}__three_pass.json.threepass_checkpoint.json').read_text())
        segments = checkpoint['segmented']
        occurrences = collections.Counter(norm(row.get('text')) for row in segments)
        wanted = {norm(row['line']): row for row in gold['entries']
                  if occurrences[norm(row['line'])] == 1
                  and row['expected_speaker'].upper() not in SPECIAL}
        windows = [window for window in make_windows(len(segments), 25)
                   if any(norm(segments[i].get('text')) in wanted for i in window)]
        if len(windows) > 40:
            stride = len(windows) / 40
            windows = [windows[int(k * stride)] for k in range(40)]
        for window in windows:
            for i in window:
                if get_deterministic_named_entry(segments[i]) is not None:
                    continue
                row = wanted.get(norm(segments[i].get('text')))
                if row:
                    expected_ids.add(book + ':' + row['id'])
    assert {book_key(book) for book in gold_by_book} == eval_books

    by_arm = {arm: {} for arm in ['base', 'lora']}
    mismatches, availability_mismatches = 0, 0
    normalized_availability = collections.Counter()
    for row in doc['rows']:
        arm, identifier = row['arm'], row['id']
        assert arm in by_arm and identifier not in by_arm[arm]
        book, gold_id = identifier.split(':', 1)
        gold = gold_by_book[book]
        key = next(g for g in gold['entries'] if g['id'] == gold_id)
        assert row['line'] == key['line']
        assert row['expected'] == key['expected_speaker'].upper()
        correct = same_speaker(key['expected_speaker'], row['predicted'], alias_groups(gold))
        mismatches += correct != row['correct']
        available = row['expected'] in row['candidates']
        availability_mismatches += available != row['in_candidates']
        normalized_availability[arm] += normalize(row['expected']) in {
            normalize(candidate) for candidate in row['candidates']}
        by_arm[arm][identifier] = dict(row, correct=correct, in_candidates=available)
    assert mismatches == availability_mismatches == 0
    assert all(set(rows) == expected_ids for rows in by_arm.values())
    assert len(expected_ids) == 5543
    for identifier in expected_ids:
        assert by_arm['base'][identifier]['candidates'] == by_arm['lora'][identifier]['candidates']

    def summarize(ids):
        arms = {}
        for arm, rows in by_arm.items():
            selected = [rows[i] for i in ids]
            arms[arm] = {'n': len(ids), 'correct': sum(r['correct'] for r in selected),
                         'unanswered': sum(not r['predicted'] for r in selected)}
            arms[arm]['accuracy'] = arms[arm]['correct'] / len(ids)
        improved = sum(not by_arm['base'][i]['correct'] and by_arm['lora'][i]['correct'] for i in ids)
        regressed = sum(by_arm['base'][i]['correct'] and not by_arm['lora'][i]['correct'] for i in ids)
        p = binomtest(min(improved, regressed), improved + regressed, 0.5).pvalue if improved + regressed else 1.0
        return {'arms': arms, 'improved': improved, 'regressed': regressed,
                'delta_percentage_points': 100 * (arms['lora']['accuracy'] - arms['base']['accuracy']),
                'exact_mcnemar_p': p}

    overall = summarize(expected_ids)
    shared = {i for i in expected_ids if all(by_arm[a][i]['predicted'] for a in by_arm)}
    strict = summarize(shared)
    for arm in by_arm:
        assert overall['arms'][arm]['correct'] == doc['summary'][arm]['correct']
        assert overall['arms'][arm]['accuracy'] == doc['summary'][arm]['accuracy']
        assert strict['arms'][arm]['correct'] == doc['strict']['arms'][arm]['correct']
        assert strict['arms'][arm]['n'] == doc['strict']['arms'][arm]['n']
        assert sum(r['in_candidates'] for r in by_arm[arm].values()) == doc['summary'][arm]['available']
    assert strict['improved'] == doc['strict']['paired']['improved']
    assert strict['regressed'] == doc['strict']['paired']['regressed']
    assert abs(strict['exact_mcnemar_p'] / doc['strict']['paired']['p'] - 1) < 1e-10
    per_book = {book: summarize({i for i in expected_ids if i.startswith(book + ':')})
                for book in sorted(gold_by_book)}
    deltas = np.array([row['delta_percentage_points'] for row in per_book.values()])
    samples = np.random.default_rng(20260930).choice(deltas, size=(20000, len(deltas)), replace=True).mean(axis=1)
    output = {'status': 'complete', 'source_result_sha256': get_sha256(args.result),
              'source_provenance': source, 'controls_passed': len(controls),
              'verification': {'rows_checked': len(doc['rows']), 'correctness_mismatches': mismatches,
                               'availability_mismatches': availability_mismatches, 'expected_ids_per_arm': len(expected_ids),
                               'frozen_files_verified': len(evaluation['files']), 'train_examples': len(train_rows),
                               'train_books': sorted(train_books), 'evaluation_books': sorted(eval_books),
                               'book_overlap': 0, 'training_data_sha256': data_hash,
                               'prompt_hashes_present': sum(bool(r['prompt_sha256']) for r in doc['rows']),
                               'raw_responses_present': sum(bool(r['raw_response']) for r in doc['rows'])},
              'overall': overall, 'shared_answered_only': strict, 'per_book': per_book,
              'book_summary': {'improved': int(sum(deltas > 0)), 'regressed': int(sum(deltas < 0)),
                               'tied': int(sum(deltas == 0)), 'macro_delta_percentage_points': float(deltas.mean()),
                               'bootstrap95_macro_delta_percentage_points': [float(x) for x in np.quantile(samples, [.025, .975])],
                               'bootstrap_draws': 20000, 'bootstrap_seed': 20260930},
              'normalized_candidate_coverage_diagnostic': dict(normalized_availability),
              'limitations': ['Book-held-out, not a verified author-held-out split.',
                              'Recomputes stored predictions; absent raw replies and prompt hashes prevent reparsing and byte-level prompt comparison.',
                              'Quote-level McNemar assumes independent pairs; report book-level bootstrap separately.',
                              'No development-book evaluation of this adapter; does not close goal 1.3 gap target.']}
    Path(args.out).write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'overall': overall, 'shared_answered_only': strict,
                      'book_summary': output['book_summary'], 'verification': output['verification']}, indent=2))


if __name__ == '__main__':
    main()
