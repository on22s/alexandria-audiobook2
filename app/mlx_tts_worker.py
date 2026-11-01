"""Apple MLX Qwen3-TTS worker (#1442). Runs in the separate MLX env, not app/env.

mlx-audio needs transformers 5.x while AA2 pins 4.x, so this file imports
nothing from AA2. Protocol: one JSON object per line on stdin, one reply per
line on stdout. Library chatter is moved to stderr so stdout stays protocol.

Request:  {"id", "op": "generate", "family": "custom"|"clone"|"design",
           "model", "text", "language", "speaker", "instruct", "ref_audio",
           "ref_text", "seed", "max_tokens", "output"}
          {"id", "op": "status"}
Reply:    {"id", "ok": true, ...} or {"id", "ok": false, "error", "type"}

Output is written to a temporary file and renamed, so a stopped worker never
leaves a partial file at `output`. Generation is never streamed (mlx-audio
#1067: streamed clones start without reference context) and is always capped
by `max_tokens` (Qwen3-TTS can otherwise run on).
"""
import json
import os
import sys

_PROTOCOL = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1, encoding='utf-8')
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
sys.stdout = sys.stderr

_model = {'path': None, 'value': None}


def get_model(path):
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model
    if _model['path'] != path:
        _model['value'] = None
        mx.clear_cache()
        _model['value'] = load_model(path)
        _model['path'] = path
    return _model['value']


def generate(request):
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    model = get_model(request['model'])
    family, text, language = request['family'], request['text'], request['language']
    if int(request.get('seed', -1)) >= 0:
        mx.random.seed(int(request['seed']))
    common = {'max_tokens': int(request['max_tokens'])}
    if family == 'custom':
        results = model.generate_custom_voice(text=text, speaker=request['speaker'], language=language,
                                              instruct=request.get('instruct') or None, **common)
    elif family == 'design':
        results = model.generate_voice_design(text=text, instruct=request['instruct'], language=language, **common)
    elif family == 'clone':
        results = model.generate(text=text, ref_audio=request['ref_audio'], ref_text=request['ref_text'],
                                 lang_code=language.lower(), **common)
    else:
        raise ValueError(f'Unknown family: {family}')
    chunks, rate = [], None
    for result in results:
        chunks.append(np.array(result.audio, dtype=np.float32).reshape(-1))
        rate = result.sample_rate
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if not len(audio) or not rate or not np.isfinite(audio).all():
        raise ValueError('MLX returned no finite audio')
    output = request['output']
    partial = f'{output}.partial'
    sf.write(partial, audio, rate, subtype='PCM_16', format='WAV')
    os.replace(partial, output)
    mx.clear_cache()
    return {'sample_rate': int(rate), 'samples': int(len(audio))}


def get_status():
    import importlib.metadata as metadata
    return {'versions': {name: metadata.version(name) for name in ('mlx-audio', 'mlx', 'transformers')},
            'model': _model['path']}


def main():
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if request.get('op') == 'status':
                reply = get_status()
            elif request.get('op') == 'generate':
                reply = generate(request)
            else:
                raise ValueError(f"Unknown op: {request.get('op')}")
            reply.update(id=request.get('id'), ok=True)
        except Exception as error:
            reply = {'id': request.get('id'), 'ok': False, 'type': type(error).__name__, 'error': str(error)[:2000]}
        _PROTOCOL.write(json.dumps(reply) + '\n')


if __name__ == '__main__':
    main()
