import hashlib
import json
import sys
from pathlib import Path
import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

parser_root = Path(sys.argv[1])
sys.path.insert(0, str(parser_root / 'app'))
from speech_verification_compare import get_comparison_tokens
root = Path(__file__).parent
out = root / 'rocm-qualification'
row = json.loads((out/'unmerged/result.json').read_text())['samples'][0]
audio, rate = sf.read(row['path'], dtype='float32')
negative_paths = {}
for name, signal in [('silence', np.zeros(rate*2, dtype='float32')),
                     ('truncated_half', audio[:len(audio)//2])]:
    path = out / (name+'.wav')
    sf.write(path, signal, rate)
    negative_paths[name] = path
model = WhisperModel(str(root/'asr-small-en'), device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
records = []
expected_ending = get_comparison_tokens(row['text'])[-2:]
for name, path in negative_paths.items():
    segments, _ = model.transcribe(str(path), language='en', beam_size=5, vad_filter=False,
                                  condition_on_previous_text=False, word_timestamps=True)
    transcript = ' '.join(segment.text.strip() for segment in segments).strip()
    observed = get_comparison_tokens(transcript)[-5:]
    ending = any(observed[index:index+2] == expected_ending for index in range(len(observed)))
    records.append({'control': name, 'transcript': transcript, 'ending_present': ending,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'expected_text': row['text'], 'source_sha256': row['sha256']})
    assert not ending, 'ASR ending scorer accepts a negative audio control'
(out/'asr-negative-controls.json').write_text(json.dumps({'state':'passed','records':records},indent=2))
print(json.dumps(records,indent=2))
