"""Verify downloaded campaign WAVs and score ASR on CPU; publish aggregates only.

Raw exports/audio/transcripts remain in --raw-root and --private-out, outside Git.
Run with HF_HOME pointing to the existing model cache and offline flags enabled.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time


def get_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--raw-root', type=Path, required=True)
    parser.add_argument('--private-out', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app))
    import numpy as np
    import soundfile as sf
    import torch
    from experiments.thunder_audio_campaign import get_audio_record
    from experiments.asr_backends import run_transformers_whisper, word_error_rate
    from utils import atomic_json_write

    assert not torch.cuda.is_available(), 'CPU verification required'
    torch.set_num_threads(4)
    controls = {}
    with tempfile.TemporaryDirectory() as folder:
        valid = Path(folder) / 'valid.wav'
        sf.write(valid, .2 * np.sin(np.arange(24000) * 2 * np.pi * 440 / 24000), 24000)
        assert get_audio_record(valid)['seconds'] == 1
        controls['one_second_sine_accepted'] = True
        for name, signal in [('silence', np.zeros(24000)), ('nonfinite', np.full(24000, np.nan))]:
            path = Path(folder) / (name + '.wav')
            sf.write(path, signal, 24000, subtype='FLOAT')
            try:
                get_audio_record(path)
            except ValueError:
                controls[name + '_rejected'] = True
            else:
                raise AssertionError(name + ' accepted')
        truncated = Path(folder) / 'truncated.wav'
        truncated.write_bytes(b'RIFF')
        try:
            get_audio_record(truncated)
        except Exception:
            controls['truncated_rejected'] = True
        else:
            raise AssertionError('truncated WAV accepted')
    for reference, hypothesis, expected in [('one two', 'one two', 0), ('one two', '', 1),
                                            ('one two', 'three four', 1), ('one', 'one two three', 2)]:
        assert word_error_rate(reference, hypothesis) == expected
    controls['wer_positive_and_three_negative_cases'] = True
    assert word_error_rate('...', 'unexpected words') is None
    controls['punctuation_only_reference_is_unscored'] = True

    pending, reports, source_hashes = [], {}, {}
    for box, name, count in [('tnr-0', 'chapter', 768), ('tnr-2', 'voices', 80)]:
        root = args.raw_root / box
        result_path = root / (name + '_run') / 'result.json'
        workload_path = root / name / 'workload.json'
        doc = json.loads(result_path.read_text())
        workload = json.loads(workload_path.read_text())
        assert doc['status'] == 'complete' and doc['completed'] == doc['requested'] == count
        assert get_sha256(workload_path) == doc['workload_sha256']
        assert len(doc['batches']) == len(workload['batches'])
        source_hashes[name + '_result'] = get_sha256(result_path)
        source_hashes[name + '_workload'] = get_sha256(workload_path)
        # Verify every downloaded human reference against the original manifest.
        for record in workload['files']:
            if record['path'].startswith('human/'):
                assert get_sha256(root / name / record['path']) == record['sha256']
        remote_run = Path(doc['batches'][0]['clips'][0]['path']).parents[2]
        health, batches, paths = [], [], set()
        for batch, expected_batch in zip(doc['batches'], workload['batches']):
            assert batch['label'] == expected_batch['label']
            expected = {chunk['index']: chunk for chunk in expected_batch['chunks']}
            assert len(expected) == len(batch['clips'])
            assert set(expected) == {clip['index'] for clip in batch['clips']}
            duration = 0
            for clip in batch['clips']:
                original = expected[clip['index']]
                assert all(clip.get(k) == original.get(k) for k in ('text', 'speaker', 'human'))
                relative = Path(clip['path']).relative_to(remote_run)
                path = (root / (name + '_run') / relative).resolve()
                assert path.is_relative_to(root.resolve()) and path not in paths
                paths.add(path)
                measured = get_audio_record(path)
                assert measured['sha256'] == clip['sha256']
                assert math.isclose(measured['seconds'], clip['seconds'], abs_tol=1e-9)
                samples, rate = sf.read(path, dtype='float32', always_2d=True)
                assert rate == 24000 and samples.shape[1] == 1
                health.append({'peak': float(np.max(np.abs(samples))),
                               'clip_fraction': float(np.mean(np.abs(samples) >= .9999))})
                duration += measured['seconds']
                pending.append({'task': name, 'label': batch['label'], 'index': clip['index'],
                                'path': str(path), 'sha256': clip['sha256'], 'text': clip['text']})
            assert math.isclose(duration, batch['audio_s'], abs_tol=1e-8)
            assert math.isfinite(batch['wall_s']) and batch['wall_s'] > 0
            assert math.isclose(batch['wall_s'] / duration, batch['generation_over_audio'])
            batches.append({'label': batch['label'], 'n': len(batch['clips']),
                            'wall_s': batch['wall_s'], 'audio_s': duration,
                            'generation_over_audio': batch['wall_s'] / duration})
        assert len(paths) == count
        assert {p.resolve() for p in (root / (name + '_run') / 'wavs').rglob('*.wav')} == paths
        wall = sum(b['wall_s'] for b in batches)
        audio = sum(b['audio_s'] for b in batches)
        assert math.isclose(wall, doc['wall_s']) and math.isclose(audio, doc['audio_s'])
        ratios = [b['generation_over_audio'] for b in batches]
        assert math.isclose(statistics.median(ratios), doc['median_batch_ratio'])
        assert math.isclose(max(ratios), doc['worst_batch_ratio'])
        reports[name] = {'n': count, 'workers': workload['workers'], 'wall_s': wall,
                         'audio_s': audio, 'generation_over_audio': wall / audio,
                         'audio_over_generation': audio / wall,
                         'max_peak_abs': max(h['peak'] for h in health),
                         'clips_with_samples_at_clip_threshold': sum(h['clip_fraction'] > 0 for h in health),
                         'max_clip_fraction': max(h['clip_fraction'] for h in health),
                         'sample_rate': 24000, 'channels': 1, 'batches': batches,
                         'generation_provenance': doc['provenance']}

    print('Verified all 848 waveforms, durations, input mapping, hashes, and timing totals', flush=True)
    cache = {}
    if args.private_out.exists():
        saved = json.loads(args.private_out.read_text())
        assert saved['source_hashes'] == source_hashes
        cache = {row['sha256']: row['hypothesis'] for row in saved['rows']}
    rows, elapsed, started = [], [], time.monotonic()
    for i, item in enumerate(pending, 1):
        start = time.monotonic()
        if word_error_rate(item['text'], '') is None:
            row = dict(item, hypothesis=None, wer=None, exclusion='no_scoreable_reference_tokens')
        else:
            if item['sha256'] not in cache:
                hypothesis, _ = run_transformers_whisper(item['path'], model_id='openai/whisper-base.en')
                cache[item['sha256']] = hypothesis
            row = dict(item, hypothesis=cache[item['sha256']],
                       wer=word_error_rate(item['text'], cache[item['sha256']]))
            assert row['wer'] is not None and math.isfinite(row['wer'])
        rows.append(row)
        elapsed.append(time.monotonic() - start)
        if i % 16 == 0 or i == len(pending):
            atomic_json_write({'status': 'complete' if i == len(pending) else 'running',
                               'source_hashes': source_hashes, 'rows': rows}, str(args.private_out))
            args.private_out.chmod(0o600)
            print(f'ASR {i}/{len(pending)}; recent seconds/clip={statistics.mean(elapsed[-16:]):.2f}', flush=True)
    for name, report in reports.items():
        all_rows = [row for row in rows if row['task'] == name]
        measured = [row for row in all_rows if row['wer'] is not None]
        report['asr'] = {'n': len(measured), 'requested': len(all_rows),
                         'excluded_no_reference_tokens': len(all_rows) - len(measured),
                         'mean_clip_wer': statistics.mean(r['wer'] for r in measured),
                         'median_clip_wer': statistics.median(r['wer'] for r in measured),
                         'clips_wer_over_0_2': sum(r['wer'] > .2 for r in measured),
                         'empty_hypotheses': sum(not r['hypothesis'].strip() for r in measured)}
        if name == 'voices':
            report['asr_by_voice_arm'] = {
                label: {'n': len(group), 'mean_clip_wer': statistics.mean(r['wer'] for r in group),
                        'median_clip_wer': statistics.median(r['wer'] for r in group),
                        'clips_wer_over_0_2': sum(r['wer'] > .2 for r in group)}
                for label in sorted({r['label'] for r in measured})
                for group in [[r for r in measured if r['label'] == label]]}
    result = {'status': 'complete', 'summary': reports, 'source_hashes': source_hashes,
              'controls': controls, 'asr_model': 'openai/whisper-base.en', 'device': 'cpu',
              'asr_elapsed_s': time.monotonic() - started,
              'verification_script_sha256': get_sha256(__file__),
              'asr_source_sha256': get_sha256(args.app / 'experiments/asr_backends.py'),
              'note': 'Mean/median WER is per clip, not pooled word-weighted WER. ASR errors include recognizer errors. '
                      'No listening, accent, alignment, or automatic promotion verdict; no local speed baseline for this workload.'}
    atomic_json_write(result, str(args.out))
    print('COMPLETE', json.dumps({k: v['asr'] for k, v in reports.items()}), flush=True)


if __name__ == '__main__':
    main()
