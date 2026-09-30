"""Render frozen real-book or unseen-voice workloads through production TTS.

Each timed call includes loading and adapter changes. Partial output is marked
running/failed, never presented as a complete measurement. No app files change.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from utils import atomic_json_write


def get_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def get_audio_record(path):
    import numpy as np
    import soundfile as sf
    samples, rate = sf.read(path)
    if not samples.size or not np.isfinite(samples).all() or not np.any(samples):
        raise ValueError(f'invalid waveform: {path}')
    seconds = len(samples) / rate
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f'invalid duration: {path}')
    return {'path': str(path), 'sha256': get_sha256(path), 'seconds': seconds,
            'sample_rate': rate}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    inputs = Path(args.inputs).resolve()
    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit('refusing to overwrite an existing campaign result')
    workload = json.loads((inputs / 'workload.json').read_text())
    for record in workload['files']:
        assert get_sha256(inputs / record['path']) == record['sha256'], record
    from experiments.provenance import provenance
    from tts import TTSEngine
    engine = TTSEngine({'tts': {'mode': 'local', 'compile_codec': False,
                              'sub_batch_enabled': True}})
    engine.set_sub_batch_size(workload['workers'])
    doc = {'status': 'running', 'workload_sha256': get_sha256(inputs / 'workload.json'),
           'provenance': provenance(__file__, args), 'batches': [],
           'requested': sum(len(b['chunks']) for b in workload['batches']),
           'completed': 0, 'note': 'Wall time includes cold load and adapter changes; '
           'batch ratios are not per-clip timing measurements.'}
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(doc, str(out))
    try:
        for i, batch in enumerate(workload['batches']):
            voices = {}
            for speaker, original in batch['voices'].items():
                voice = dict(original)
                if voice.get('adapter_path'):
                    voice['adapter_path'] = str(inputs / voice['adapter_path'])
                voices[speaker] = voice
            folder = out.parent / 'wavs' / str(i)
            folder.mkdir(parents=True, exist_ok=False)
            start = time.monotonic()
            result = engine.generate_batch(batch['chunks'], voices, str(folder),
                                           batch_seed=1234)
            wall = time.monotonic() - start
            expected = {chunk['index'] for chunk in batch['chunks']}
            if result['failed'] or set(result['completed']) != expected:
                raise RuntimeError(f'incomplete batch {i}: {result}')
            clips = [dict(get_audio_record(folder / f"temp_batch_{c['index']}.wav"),
                          index=c['index'], speaker=c['speaker'], text=c['text'],
                          human=c.get('human')) for c in batch['chunks']]
            audio = sum(c['seconds'] for c in clips)
            doc['batches'].append({'label': batch['label'], 'wall_s': wall,
                                   'audio_s': audio, 'generation_over_audio': wall / audio,
                                   'clips': clips})
            doc['completed'] += len(clips)
            atomic_json_write(doc, str(out))
            print(f"PROGRESS {doc['completed']}/{doc['requested']} batch={i} "
                  f"wall={wall:.1f}s audio={audio:.1f}s ratio={wall/audio:.3f}", flush=True)
        ratios = [b['generation_over_audio'] for b in doc['batches']]
        doc.update(status='complete', wall_s=sum(b['wall_s'] for b in doc['batches']),
                   audio_s=sum(b['audio_s'] for b in doc['batches']),
                   median_batch_ratio=statistics.median(ratios), worst_batch_ratio=max(ratios))
    except Exception as exc:
        doc.update(status='failed', error=str(exc))
        raise
    finally:
        atomic_json_write(doc, str(out))


if __name__ == '__main__':
    main()
