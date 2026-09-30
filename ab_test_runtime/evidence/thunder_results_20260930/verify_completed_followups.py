"""Verify private completed Chinese/voice evidence; publish aggregate fields only."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import numpy as np
import soundfile as sf


def get_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_json(path):
    return json.loads(Path(path).read_text())


def verify_audio(path, record):
    assert get_sha256(path) == record['sha256']
    samples, rate = sf.read(path, always_2d=True)
    assert samples.size and np.isfinite(samples).all() and np.any(samples)
    assert rate == 24000 and samples.shape[1] == 1
    assert abs(len(samples) / rate - record['seconds']) < 1e-9


def get_expected_human(arms):
    return 'Both' if arms == ['human', 'human'] else str(arms.index('human') + 1)


def verify_ratings(root, folder, key_name, ratings_name):
    package = get_json(root / folder / 'package_summary.json')
    key_path = root / 'keys' / key_name
    assert get_sha256(key_path) == package['key_sha256']
    assert get_sha256(root / folder / 'listen.html') == package['html_sha256']
    key = get_json(key_path)['pairs']
    ratings = get_json(root / ratings_name)
    expected_package = ('thunder_voice_retest_20260930' if folder == 'voice_retest'
                        else 'thunder_voice_diagnostic_20260930')
    assert ratings['package'] == expected_package
    assert set(ratings['ratings']) == {str(x['slot']) for x in key}
    rows = []
    for item in key:
        for f in item['files']:
            assert get_sha256(f['path']) == f['sha256']
        rating = ratings['ratings'][str(item['slot'])]
        value = rating.get('same_voice')
        assert value in (None, '', '1', '2', '3', '4', '5')
        assert rating['human'] in ('1', '2', 'Both', 'Cannot tell')
        rows.append({'adapter': item['adapter'], 'line_index': item.get('line_index', 0),
                     'seed': item['seed'], 'kind': item['kind'],
                     'similarity': int(value) if value else None,
                     'human_correct': rating['human'] == get_expected_human(item['arms']),
                     'human_choice': rating['human']})
    groups = {}
    for kind in sorted({x['kind'] for x in rows}):
        rr = [x for x in rows if x['kind'] == kind]
        groups[kind] = {'pairs': len(rr), 'similarity_rated': sum(x['similarity'] is not None for x in rr),
                        'similarity_histogram': dict(Counter(str(x['similarity']) for x in rr if x['similarity'] is not None)),
                        'human_correct': sum(x['human_correct'] for x in rr),
                        'human_choices': dict(Counter(x['human_choice'] for x in rr))}
    return {'rated_at': ratings['rated_at'], 'raters': 1, 'groups': groups,
            'ratings_sha256': get_sha256(root / ratings_name),
            'sealed_key_sha256': package['key_sha256'], 'html_sha256': package['html_sha256'],
            'main_rows': [x for x in rows if x['kind'] == 'main']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('chinese-root', 'voice-root', 'retest-root', 'out'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    assert get_expected_human(['human', 'generated']) == '1'
    assert get_expected_human(['generated', 'human']) == '2'
    assert get_expected_human(['human', 'human']) == 'Both'
    assert get_expected_human(['generated', 'human']) != '1'
    chinese = get_json(a.chinese_root / 'result_final_private.json')
    cases = chinese['cases']; assert len(cases) == 300
    for row in cases:
        verify_audio(a.chinese_root / 'wavs' / Path(row['audio']['path']).name, row['audio'])
        ratio = row['audio']['seconds'] / row['human_seconds']
        assert math.isclose(ratio, row['duration_ratio'], rel_tol=1e-8)
        assert (ratio > 3) == (row['status'] == 'rejected')
    arms = {}
    by_arm = {}
    for arm in ('long_typical', 'short_original'):
        rr = [x for x in cases if x['arm'] == arm]; assert len(rr) == 150
        by_arm[arm] = {x['id']: x for x in rr}; assert len(by_arm[arm]) == 150
        rejected = sum(x['status'] == 'rejected' for x in rr)
        arms[arm] = {'attempted': 150, 'duration_rejected': rejected, 'rejection_percent': rejected / 150 * 100,
                     'median_duration_ratio': statistics.median(x['duration_ratio'] for x in rr),
                     'maximum_duration_ratio': max(x['duration_ratio'] for x in rr),
                     'maximum_audio_seconds': max(x['audio']['seconds'] for x in rr)}
    long, short = by_arm['long_typical'], by_arm['short_original']
    assert set(long) == set(short)
    assert all(long[k]['text'] == short[k]['text'] and long[k]['human_seconds'] == short[k]['human_seconds']
               and long[k]['seed'] == short[k]['seed'] for k in long)
    voice = get_json(a.voice_root / 'result_private.json')
    assert voice['status'] == 'complete' and voice['completed'] == voice['requested'] == len(voice['rows']) == 24
    old = get_json(a.retest_root / 'result_private.json')
    repeat_matches = 0
    for row in voice['rows']:
        verify_audio(a.voice_root / 'renders' / Path(row['path']).name, row)
        if row['line_index'] == 0:
            prior = next(x for x in old['rows'] if x['kind'] == 'main' and x['target_adapter'] == row['adapter'] and x['seed'] == row['seed'])
            repeat_matches += row['sha256'] == prior['sha256']
    quality = get_json(a.voice_root / 'quality_private.json')
    assert quality['status'] == 'complete' and len(quality['rows']) == 24 and len(quality['human_controls']) == 12
    acoustic = [{k: x[k] for k in ('adapter', 'line_index', 'seed', 'wer', 'ecapa', 'seconds', 'duration_ratio', 'pitch')}
                for x in quality['rows']]
    result = {'status': 'complete', 'experiment': 'thunder_completed_followups',
              'runtime_commit': '454417276a323649acb6c2c98a02f97acd2edfaf',
              'chinese': {'verified_waveforms': 300, 'paired_lines': 150, 'duration_gate_max_ratio': 3,
                          'arms': arms, 'long_rejected_short_passed': sum(long[k]['status'] == 'rejected' and short[k]['status'] == 'complete' for k in long),
                          'raw_result_sha256': get_sha256(a.chinese_root / 'result_final_private.json')},
              'diagnostic': {'verified_waveforms': 24, 'original_sentence_repeats': 8, 'exact_repeat_hash_matches': repeat_matches,
                             'training_performed': False, 'stored_acoustic_metrics': acoustic,
                             'metric_controls': quality['controls'],
                             'human_asr_mean_wer': statistics.mean(x['wer'] for x in quality['human_controls']),
                             'result_sha256': get_sha256(a.voice_root / 'result_private.json'),
                             'quality_sha256': get_sha256(a.voice_root / 'quality_private.json')},
              'retest_listening': verify_ratings(a.retest_root, 'voice_retest', 'voice_retest_key_private.json', 'voice_retest_ratings_private.json'),
              'diagnostic_listening': verify_ratings(a.voice_root, 'diagnostic_listening', 'diagnostic_key_private.json', 'voice_diagnostic_ratings_private.json'),
              'limits': ['Chinese verification measures duration only, not recognition or speaker quality.',
                         'Acoustic metrics retained from CPU scorer; this verifier checks waveforms and aggregates rather than rerunning ASR, ECAPA or pitch.',
                         'Single rater, enriched listening selections; no population estimate or goal closure.',
                         'Retest similarity score 17 missing and left unscored; detailed notes remain private.']}
    a.out.write_text(json.dumps(result, indent=2) + '\n')
    print('Verified 300 Chinese and 24 diagnostic waveforms and both sealed listening packages.')


if __name__ == '__main__':
    main()
