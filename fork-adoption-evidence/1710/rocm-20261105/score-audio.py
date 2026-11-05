import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(args.repo) / 'app'))
from faster_whisper import WhisperModel
from speech_verification_compare import get_comparison_tokens, get_speech_comparison
from qualification_metrics import get_edit_distance, run_controls

root = Path(__file__).parent
out = root / 'rocm-qualification'
controls = run_controls()
model = WhisperModel(str(root / 'asr-small-en'), device='cpu', compute_type='int8', cpu_threads=4,
                     local_files_only=True)
results = {'state': 'running', 'controls': controls, 'decoder': 'faster-whisper-1.2.1-small.en-cpu-int8',
           'decoder_sha256': hashlib.sha256((root/'asr-small-en/model.bin').read_bytes()).hexdigest(),
           'settings': {'language': 'en', 'beam_size': 5, 'vad_filter': False,
                        'condition_on_previous_text': False, 'initial_prompt': None}, 'samples': []}
existing = out / 'asr-results.json'
if existing.is_file():
    old = json.loads(existing.read_text(encoding='utf-8'))
    assert old['decoder_sha256'] == results['decoder_sha256'] and old['settings'] == results['settings']
    results['samples'] = old['samples']


def checkpoint():
    temporary = existing.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(results, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, existing)

for arm in ('no_lora', 'merged', 'unmerged'):
    result_path = out / arm / 'result.json'
    if not result_path.is_file():
        continue
    source = json.loads(result_path.read_text(encoding='utf-8'))
    for row in source['samples']:
        key = (arm, row['text_id'], row['seed'])
        matches = [prior for prior in results['samples']
                   if (prior['arm'], prior['text_id'], prior['seed']) == key]
        if matches:
            assert len(matches) == 1 and matches[0]['sha256'] == row['sha256']
            continue
        assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256']
        started = time.monotonic()
        segments, info = model.transcribe(row['path'], language='en', beam_size=5, vad_filter=False,
            condition_on_previous_text=False, word_timestamps=True)
        segments = list(segments)
        transcript = ' '.join(segment.text.strip() for segment in segments).strip()
        expected = get_comparison_tokens(row['text'])
        observed = get_comparison_tokens(transcript)
        endings = expected[-2:]
        suffix = observed[-5:]
        ending_present = any(suffix[index:index+len(endings)] == endings for index in range(len(suffix)))
        result = {'arm': arm, 'text_id': row['text_id'], 'seed': row['seed'], 'sha256': row['sha256'],
                  'transcript': transcript, 'expected': row['text'], 'expected_words': len(expected),
                  'edit_distance': get_edit_distance(expected, observed), 'ending_present': ending_present,
                  'comparison': get_speech_comparison(row['text'], transcript),
                  'seconds': time.monotonic()-started,
                  'words': [{'word': word.word, 'start': word.start, 'end': word.end,
                             'probability': word.probability}
                            for segment in segments for word in (segment.words or [])]}
        results['samples'].append(result)
        checkpoint()
        print(json.dumps({key: result[key] for key in ['arm','text_id','seed','edit_distance','ending_present','transcript']}), flush=True)
results['state'] = 'done' if len(results['samples']) == 36 else 'partial'
checkpoint()
