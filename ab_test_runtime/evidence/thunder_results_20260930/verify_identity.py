"""Re-score held-out voice pairs with ECAPA after known-case controls pass.

Publishes scores/aggregates, never human recordings or reference transcripts.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess


def get_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_ecapa_scoring(pairs, python, worker, log):
    env = dict(os.environ, CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='4',
               MKL_NUM_THREADS='4', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    result = subprocess.run([python, str(worker)], input=json.dumps(pairs),
                            text=True, capture_output=True, env=env, timeout=900)
    with log.open('a') as handle:
        handle.write(result.stderr)
    log.chmod(0o600)
    if result.returncode:
        raise RuntimeError('ECAPA worker failed; inspect private log')
    values = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(values) == len(pairs)
    assert all(v is not None and math.isfinite(v) for v in values)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--raw-root', type=Path, required=True)
    parser.add_argument('--speaker-python', required=True)
    parser.add_argument('--private-log', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = args.raw_root / 'tnr-2'
    result_path = root / 'voices_run/result.json'
    doc = json.loads(result_path.read_text())
    saved = json.loads((root / 'voices_run/identity_scores.json').read_text())
    worker = args.app / 'experiments/_ecapa_batch.py'
    assert doc['status'] == saved['status'] == 'complete'
    assert doc['completed'] == doc['requested'] == 80
    assert saved['render_result_sha256'] == get_sha256(result_path)
    assert saved['scorer_sha256'] == get_sha256(worker)
    assert len(saved['scores']) == 80 and min(saved['self_controls']) > .999
    for label, summary in saved['summary'].items():
        group = [v for tag, v in saved['scores'] if tag == label]
        assert len(group) == summary['n'] == 20
        assert statistics.median(group) == summary['median_ecapa']
        assert min(group) == summary['minimum_ecapa']
        assert summary['passes_identity'] == (statistics.median(group) >= .45)

    humans = sorted((root / 'voices/human').iterdir())
    assert len(humans) == 2
    references = [sorted(folder.glob('*.wav')) for folder in humans]
    assert all(len(group) == 20 for group in references)
    # Different speakers according to the frozen workload's voice assignments.
    # Identity is assessed over known human labels before generated pairs.
    control_pairs = [[str(group[0]), str(group[0])] for group in references]
    control_pairs += [[str(group[i]), str(group[(i + 1) % 20])]
                      for group in references for i in range(20)]
    control_pairs += [[str(references[0][i]), str(references[1][i])] for i in range(20)]
    control_values = run_ecapa_scoring(control_pairs, args.speaker_python, worker, args.private_log)
    controls = {'self': control_values[:2],
                'same_speaker_different_clip_medians': [statistics.median(control_values[2:22]),
                                                        statistics.median(control_values[22:42])],
                'different_speaker_median': statistics.median(control_values[42:]),
                'different_speaker_max': max(control_values[42:]),
                'different_speaker_pairs_below_threshold': sum(v < .45 for v in control_values[42:]),
                'different_speaker_n': 20, 'threshold': .45}
    assert min(controls['self']) > .999
    assert min(controls['same_speaker_different_clip_medians']) >= .45
    assert controls['different_speaker_median'] < .45
    print('62 known-case ECAPA controls passed', flush=True)

    pairs, labels = [], []
    remote_run = Path(doc['batches'][0]['clips'][0]['path']).parents[2]
    for batch in doc['batches']:
        for clip in batch['clips']:
            relative = Path(clip['path']).relative_to(remote_run)
            pairs.append([str(root / 'voices' / clip['human']), str(root / 'voices_run' / relative)])
            labels.append(batch['label'])
    values = run_ecapa_scoring(pairs, args.speaker_python, worker, args.private_log)
    assert [tag for tag, _ in saved['scores']] == labels
    maximum_delta = max(abs(value - previous) for value, (_, previous) in zip(values, saved['scores']))
    summary = {}
    for label in sorted(set(labels)):
        group = [v for tag, v in zip(labels, values) if tag == label]
        assert len(group) == 20
        summary[label] = {'n': 20, 'median_ecapa': statistics.median(group),
                          'minimum_ecapa': min(group), 'mean_ecapa': statistics.mean(group),
                          'identity_threshold': .45, 'passes_identity_median': statistics.median(group) >= .45,
                          'clips_below_threshold': sum(v < .45 for v in group)}
    paired = {}
    for voice in ['silky_mezzo_30s_f', 'silky_alto_40s_f_literary_2']:
        shipped = [v for tag, v in zip(labels, values) if tag == voice + '_shipped']
        clean = [v for tag, v in zip(labels, values) if tag == voice + '_clean']
        differences = [c - s for s, c in zip(shipped, clean)]
        paired[voice] = {'n': 20, 'median_paired_cosine_delta_clean_minus_shipped': statistics.median(differences),
                         'mean_paired_cosine_delta_clean_minus_shipped': statistics.mean(differences),
                         'clean_higher_count': sum(d > 0 for d in differences)}
    model_root = args.app.parent / 'ab_test_runtime/ecapa'
    model_hashes = {p.name: get_sha256(p) for p in model_root.iterdir()
                    if p.is_file() and p.suffix in ('.ckpt', '.yaml')}
    report = {'status': 'complete', 'summary': summary, 'paired': paired, 'controls': controls,
              'scores': [{'label': label, 'ordinal': i % 20, 'cosine': value}
                         for i, (label, value) in enumerate(zip(labels, values))],
              'render_result_sha256': get_sha256(result_path), 'scorer_sha256': get_sha256(worker),
              'verification_script_sha256': get_sha256(__file__), 'model_hashes': model_hashes,
              'max_absolute_delta_vs_remote_scores': maximum_delta,
              'note': 'Two speakers, 20 shared held-out human clips per speaker, four rendering arms. '
                      'The .45 threshold is an existing screening policy, not calibrated universal speaker accuracy. '
                      'No listening assessment, training, weight promotion, or 80 independent speakers.'}
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'summary': summary, 'paired': paired, 'controls': controls,
                      'max_absolute_delta_vs_remote_scores': maximum_delta}), flush=True)


if __name__ == '__main__':
    main()
