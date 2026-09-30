"""Verify private ratings, sealed chapter key, and completed existing-voice retest.

Raw-root contains chapter_ratings_private.json, keys/, chapter_comparison/,
inputs/, renders/, result_private.json and pitch_summary.json. Output retains
counts and acoustic measurements only, never corpus text, audio or free notes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf


def get_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_preference(arms, preference):
    if preference == 'Tie':
        return 'Tie'
    if preference not in ('1', '2') or len(arms) != 2:
        raise ValueError('invalid preference or pair')
    return arms[int(preference) - 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = args.raw_root
    # Known accepting/rejecting cases, including shuffled control arms.
    assert get_preference(['workers16', 'workers4'], '2') == 'workers4'
    assert get_preference(['identical', 'identical'], 'Tie') == 'Tie'
    assert get_preference(['truncated', 'intact'], '1') != 'intact'
    try:
        get_preference(['workers4', 'workers16'], 'Unrated')
    except ValueError:
        pass
    else:
        raise AssertionError('unrated preference was accepted')
    ratings = json.loads((root / 'chapter_ratings_private.json').read_text())
    key_path = root / 'keys/chapter_key_private.json'
    key = json.loads(key_path.read_text())
    package = json.loads((root / 'chapter_comparison/package_summary.json').read_text())
    assert ratings['package'] == 'thunder_chapter_listening_20260930'
    assert get_sha256(key_path) == package['key_sha256']
    assert get_sha256(root / 'chapter_comparison/listen.html') == package['html_sha256']
    assert len(key['pairs']) == 24
    assert set(ratings['ratings']) == {str(p['slot']) for p in key['pairs']}
    main_rows, controls = [], []
    for pair in key['pairs']:
        rating = ratings['ratings'][str(pair['slot'])]
        assert rating['same_speaker'] in ('Yes', 'No', 'Unsure')
        selected = get_preference(pair['arms'], rating['preference'])
        if pair['selection'].endswith('control'):
            expected = 'Tie' if pair['selection'] == 'same_control' else 'intact'
            controls.append({'kind': pair['selection'], 'passed': selected == expected})
        else:
            assert set(pair['arms']) == {'workers4', 'workers16'}
            main_rows.append({'selection': pair['selection'], 'preferred': selected,
                              'same_speaker': rating['same_speaker']})
    manifest = json.loads((root / 'inputs/manifest.json').read_text())
    for item in manifest['files']:
        assert get_sha256(root / 'inputs' / item['path']) == item['sha256']
    retest = json.loads((root / 'result_private.json').read_text())
    assert retest['status'] == 'complete'
    assert retest['manifest_sha256'] == get_sha256(root / 'inputs/manifest.json')
    assert retest['completed'] == retest['requested'] == len(retest['rows']) == 16
    durations = []
    for row in retest['rows']:
        assert row['status'] == 'complete'
        path = root / 'renders' / Path(row['path']).name
        assert get_sha256(path) == row['sha256']
        samples, rate = sf.read(path, always_2d=True)
        assert samples.size and np.isfinite(samples).all() and np.any(samples)
        assert rate == 24000 and samples.shape[1] == 1
        assert abs(len(samples) / rate - row['seconds']) < 1e-9
        if row['kind'] == 'main':
            durations.append(row['seconds'])
    assert len(durations) == 12
    pitch = json.loads((root / 'pitch_summary.json').read_text())
    assert pitch['status'] == 'complete' and len(pitch['rows']) == 6
    voice_package = json.loads((root / 'voice_retest/package_summary.json').read_text())
    assert get_sha256(root / 'voice_retest/listen.html') == voice_package['html_sha256']
    result = {
        'status': 'complete', 'experiment': 'thunder_listening_followup',
        'runtime_commit': '454417276a323649acb6c2c98a02f97acd2edfaf',
        'chapter': {
            'rated_at': ratings['rated_at'], 'raters': 1, 'rated_pairs': 24,
            'main_pairs': len(main_rows), 'preferences': dict(Counter(r['preferred'] for r in main_rows)),
            'same_speaker': dict(Counter(r['same_speaker'] for r in main_rows)),
            'by_selection': {g: dict(Counter(r['preferred'] for r in main_rows if r['selection'] == g))
                             for g in ('worsened', 'improved', 'random')},
            'controls': controls, 'ratings_sha256': get_sha256(root / 'chapter_ratings_private.json'),
            'sealed_key_sha256': get_sha256(key_path), 'html_sha256': package['html_sha256'],
            'limits': 'One rater, one chapter, enriched problem-case selection; preference counts do not establish equivalence or population failure rates. Deliberate truncation controls excluded.'},
        'retest': {
            'completed_valid_waveforms': 16, 'main_renders': 12, 'foreign_voice_controls': 4,
            'new_generation_seeds': [20260930, 20261001], 'training_performed': False,
            'main_duration_range_s': [min(durations), max(durations)],
            'pitch': pitch, 'blind_package_pairs': voice_package['pairs'], 'listening_ratings_complete': False,
            'result_sha256': get_sha256(root / 'result_private.json'),
            'inputs_manifest_sha256': get_sha256(root / 'inputs/manifest.json'),
            'pitch_sha256': get_sha256(root / 'pitch_summary.json'),
            'limits': 'Valid waveform does not mean acceptable speech. Pitch does not establish perceived sex, speaker identity or audible cracking. New listening ratings pending.'},
        'verifier_sha256': get_sha256(__file__),
        'privacy': 'Raw text, notes, audio, keys and weights remain private; no Chunagon material.'}
    args.out.write_text(json.dumps(result, indent=2) + '\n')
    print('Verified 24 chapter ratings, four controls, and 16 downloaded retest WAVs.')


if __name__ == '__main__':
    main()
