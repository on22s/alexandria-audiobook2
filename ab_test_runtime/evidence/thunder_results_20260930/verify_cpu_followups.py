"""Replay historical PDNC scores and classify saved A100 errors on CPU."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys
import tarfile


def get_sha256(data):
    return hashlib.sha256(data).hexdigest()


def get_category(row, groups, same_speaker):
    if not row['predicted']:
        return 'unanswered'
    if row['correct']:
        return 'correct'
    available = any(same_speaker(row['expected'], name, groups)
                    for name in row['candidates'])
    return 'wrong_with_gold_in_roster' if available else 'wrong_without_gold_in_roster'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--archives', type=Path, nargs=2, required=True)
    parser.add_argument('--sun-fixture', type=Path, required=True)
    parser.add_argument('--a100-result', type=Path, required=True)
    parser.add_argument('--a100-fixtures', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / 'app'))
    from experiments.scoring import alias_groups, same_speaker

    # Reject wrong names even when the correct name is somewhere in the roster.
    controls = [(None, False, ['SMITH'], 'unanswered'),
                ('SMITH', True, [], 'correct'),
                ('JONES', False, ['SMITH'], 'wrong_with_gold_in_roster'),
                ('JONES', False, ['JONES'], 'wrong_without_gold_in_roster'),
                ('JONES', False, ['BOB'], 'wrong_with_gold_in_roster')]
    for predicted, correct, candidates, expected in controls:
        row = {'predicted': predicted, 'correct': correct,
               'expected': 'SMITH', 'candidates': candidates}
        assert get_category(row, [{'SMITH', 'BOB'}], same_speaker) == expected
    assert same_speaker('SMITH', 'JONES', []) is False
    assert same_speaker('MR. SMITH', 'MR SMITH', []) is True

    fixtures = {}
    fixture_hashes = {}
    for path in (args.repo / 'app/fixtures').glob('attribution_gold_pdnc_*.json'):
        book = path.stem.removeprefix('attribution_gold_')
        if '_w' not in book.split('pdnc_', 1)[1]:
            fixtures[book] = json.loads(path.read_bytes())
            fixture_hashes[book] = get_sha256(path.read_bytes())
    fixtures['pdnc_thesunalsorises'] = json.loads(args.sun_fixture.read_bytes())
    fixture_hashes['pdnc_thesunalsorises'] = get_sha256(args.sun_fixture.read_bytes())
    history = []
    for archive in args.archives:
        with tarfile.open(archive) as stream:
            for member in stream:
                if not member.isfile():
                    continue
                data = stream.extractfile(member).read()
                document = json.loads(data)
                if 'summary' not in document:
                    continue
                arms = {}
                for arm, summary in document['summary'].items():
                    rows = [r for r in document['rows'] if r['arm'] == arm]
                    assert len(rows) == summary['n']
                    assert sum(r['correct'] is True for r in rows) == summary['correct']
                    assert abs(summary['accuracy'] - summary['correct'] / len(rows)) < 1e-10
                    correct = summary['correct']
                    covered = moved_up = moved_down = 0
                    for row in rows:
                        book, identifier = row['id'].split(':', 1)
                        if not book.startswith('pdnc_'):
                            continue
                        assert book in fixtures, book
                        gold = fixtures[book]
                        key = next(g for g in gold['entries'] if g['id'] == identifier)
                        assert row['expected'].upper() == key['expected_speaker'].upper()
                        assert row['line'] == key['line']
                        now = same_speaker(key['expected_speaker'], row['predicted'], alias_groups(gold))
                        covered += 1
                        moved_up += now and not row['correct']
                        moved_down += row['correct'] and not now
                        correct += int(now) - int(row['correct'])
                    arms[arm] = {'n': len(rows), 'stored_correct': summary['correct'],
                                 'current_correct': correct, 'pdnc_rows_rescored': covered,
                                 'moved_up': moved_up, 'moved_down': moved_down}
                history.append({'report': Path(member.name).name,
                                'source_sha256': get_sha256(data), 'arms': arms})
    assert len(history) == 23

    data = args.a100_result.read_bytes()
    prior = json.loads((args.repo / 'ab_test_runtime/experiments/thunder_attribution_verification__20260930.json').read_text())
    assert get_sha256(data) == prior['source_result_sha256']
    document = json.loads(data)
    a100_gold = {}
    a100_hashes = {}
    for book, digest in document['meta']['gold_files'].items():
        path = args.a100_fixtures / f'attribution_gold_{book}.json'
        assert get_sha256(path.read_bytes()) == digest
        a100_hashes[book] = digest
        a100_gold[book] = json.loads(path.read_bytes())
    arms = {arm: {} for arm in ('base', 'lora')}
    categories = {arm: collections.Counter() for arm in arms}
    candidate_coverage = collections.Counter()
    for row in document['rows']:
        arm, identifier = row['arm'], row['id']
        assert identifier not in arms[arm]
        book, gold_id = identifier.split(':', 1)
        gold = a100_gold[book]
        key = next(g for g in gold['entries'] if g['id'] == gold_id)
        assert row['expected'] == key['expected_speaker'].upper() and row['line'] == key['line']
        groups = alias_groups(gold)
        assert same_speaker(key['expected_speaker'], row['predicted'], groups) == row['correct']
        category = get_category(row, groups, same_speaker)
        arms[arm][identifier] = (row, category)
        categories[arm][category] += 1
        candidate_coverage[arm] += any(same_speaker(row['expected'], c, groups) for c in row['candidates'])
    assert set(arms['base']) == set(arms['lora']) and len(arms['base']) == 5543
    for arm in arms:
        assert categories[arm]['correct'] == prior['overall']['arms'][arm]['correct']
        assert categories[arm]['unanswered'] == prior['overall']['arms'][arm]['unanswered']
    transitions = collections.Counter()
    regressions = collections.Counter()
    fixes = collections.Counter()
    per_book = {}
    for identifier, (base, before) in arms['base'].items():
        lora, after = arms['lora'][identifier]
        assert base['candidates'] == lora['candidates']
        transitions[before + ' -> ' + after] += 1
        book = identifier.split(':', 1)[0]
        counts = per_book.setdefault(book, collections.Counter())
        counts['n'] += 1
        counts['base_correct'] += base['correct']
        counts['lora_correct'] += lora['correct']
        counts['base_unanswered'] += before == 'unanswered'
        counts['lora_unanswered'] += after == 'unanswered'
        if base['correct'] and not lora['correct']:
            regressions[after] += 1
            counts['regressions'] += 1
            counts['regressions_to_unanswered'] += after == 'unanswered'
        if not base['correct'] and lora['correct']:
            fixes[before] += 1
            counts['fixes'] += 1
    assert sum(regressions.values()) == 549 and sum(fixes.values()) == 895
    output = {'status': 'complete', 'controls_passed': 7,
              'verifier_sha256': get_sha256(Path(__file__).read_bytes()),
              'scoring_sha256': get_sha256((args.repo / 'app/experiments/scoring.py').read_bytes()),
              'historical': {'reports': history, 'fixture_sha256': fixture_hashes,
                             'scope': 'All PDNC rows rescored; non-PDNC rows retain stored flags.'},
              'a100': {'source_sha256': get_sha256(data), 'fixture_sha256': a100_hashes,
                       'n_per_arm': 5543, 'categories': categories,
                       'alias_aware_candidate_coverage': candidate_coverage,
                       'transitions': transitions, 'regression_destinations': regressions,
                       'fix_origins': fixes, 'per_book': per_book},
              'limitations': ['Stored prediction analysis, not model inference or raw-response replay.',
                              'Roster coverage diagnoses saved samples; categories do not prove causes.',
                              'Historical alias replay does not establish valid serving or training separation.']}
    args.out.write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'historical_reports': len(history), 'a100_categories': categories,
                      'regression_destinations': regressions, 'fix_origins': fixes}))


if __name__ == '__main__':
    main()
