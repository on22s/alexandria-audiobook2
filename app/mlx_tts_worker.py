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

Clone reference work is cached by content (#1438), never by speaker label:
prepared audio by (file bytes, sample rate); mlx-audio's reference codes and
transcript tokens by (file bytes, transcript); the speaker embedding by file
bytes. Every cache is bounded by entries and bytes and is cleared with the model.
"""
from collections import OrderedDict
import hashlib
import json
import os
import sys

_PROTOCOL = None
_model = {'path': None, 'value': None}
MIB = 1024 * 1024


def array_bytes(value):
    """Retained bytes of an array or a tuple of arrays (unknown types count as 0)."""
    if isinstance(value, (tuple, list)):
        return sum(array_bytes(item) for item in value)
    return int(getattr(value, 'nbytes', 0) or 0)


class BoundedCache:
    """Least-recently-used map bounded by entry count and retained bytes."""

    def __init__(self, max_entries, max_bytes, size=array_bytes):
        self.max_entries, self.max_bytes, self._size = max_entries, max_bytes, size
        self._items = OrderedDict()
        self.bytes = self.hits = self.misses = self.evictions = 0

    def get(self, key):
        if key in self._items:
            self._items.move_to_end(key)
            self.hits += 1
            return self._items[key][0]
        self.misses += 1
        return None

    def put(self, key, value):
        size = self._size(value)
        if size > self.max_bytes:
            return  # too large to retain; the caller still uses it once
        if key in self._items:
            self.bytes -= self._items.pop(key)[1]
        self._items[key] = (value, size)
        self.bytes += size
        while len(self._items) > self.max_entries or self.bytes > self.max_bytes:
            self.bytes -= self._items.popitem(last=False)[1][1]
            self.evictions += 1

    def __contains__(self, key):
        return key in self._items

    def clear(self):
        self._items.clear()
        self.bytes = 0

    def stats(self):
        # Field names match AA2's record_tts_reference_cache diagnostics.
        return {'entries': len(self._items), 'charged_bytes': self.bytes, 'hits': self.hits,
                'misses': self.misses, 'evictions': self.evictions,
                'max_entries': self.max_entries, 'max_bytes': self.max_bytes}


class ScopedICLCache:
    """Stands in for mlx-audio's unbounded `model._icl_cache`.

    mlx-audio keys reference codes by (transcript, (sample count, sample sum)),
    so two different clips with equal length and sum would share codes. Each
    key is prefixed with the content digest of the reference being rendered.
    With no digest in scope nothing is cached or reused.
    """

    def __init__(self, cache):
        self.cache, self.scope = cache, None

    def __contains__(self, key):
        return self.scope is not None and (self.scope, key) in self.cache

    def __getitem__(self, key):
        value = self.cache.get((self.scope, key)) if self.scope is not None else None
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        # mlx-audio tests `in` before reading and stores only after a miss.
        if self.scope is not None:
            self.cache.misses += 1
            self.cache.put((self.scope, key), value)


class ReferenceCaches:
    """All clone-reference caches for one loaded model."""

    def __init__(self):
        self.prepared = BoundedCache(16, 64 * MIB)
        self.icl = BoundedCache(32, 64 * MIB)
        self.speaker = BoundedCache(32, 16 * MIB)
        self.icl_view = ScopedICLCache(self.icl)
        self._current = None  # (digest, prepared array) of the request being rendered

    def attach(self, model):
        """Route the model's own reference caches through these bounded ones."""
        if hasattr(model, '_icl_cache'):
            model._icl_cache = self.icl_view
        extract = getattr(model, 'extract_speaker_embedding', None)
        if extract is not None:
            def cached_extract(audio, sr=24000):
                current = self._current
                if current is None or audio is not current[1]:
                    return extract(audio, sr)
                key = (current[0], sr)
                embedding = self.speaker.get(key)
                if embedding is None:
                    embedding = extract(audio, sr)
                    self.speaker.put(key, embedding)
                return embedding
            model.extract_speaker_embedding = cached_extract

    def prepare(self, path, sample_rate, load):
        """Prepared reference array for `path`, keyed by its bytes, not its name."""
        with open(path, 'rb') as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        key = (digest, int(sample_rate))
        audio = self.prepared.get(key)
        if audio is None:
            audio = load(path, sample_rate)
            self.prepared.put(key, audio)
        return digest, audio

    def begin(self, digest, audio):
        self._current = (digest, audio)
        self.icl_view.scope = digest

    def end(self):
        self._current = None
        self.icl_view.scope = None

    def clear(self):
        self.end()
        for cache in (self.prepared, self.icl, self.speaker):
            cache.clear()

    def stats(self):
        return {'prepared_audio': self.prepared.stats(), 'reference_codes': self.icl.stats(),
                'speaker_embedding': self.speaker.stats()}


_caches = ReferenceCaches()


def get_model(path):
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model
    if _model['path'] != path:
        _model['value'] = None
        _caches.clear()
        mx.clear_cache()
        _model['value'] = load_model(path)
        _model['path'] = path
        _caches.attach(_model['value'])
    return _model['value']


def load_reference(path, sample_rate):
    import mlx.core as mx
    from mlx_audio.utils import load_audio
    audio = load_audio(path, sample_rate=sample_rate)
    mx.eval(audio)
    return audio


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
        digest, ref_audio = _caches.prepare(request['ref_audio'], model.sample_rate, load_reference)
        _caches.begin(digest, ref_audio)
        try:
            # generate() yields lazily, so the scope has to cover consuming it too.
            results = list(model.generate(text=text, ref_audio=ref_audio, ref_text=request['ref_text'],
                                          lang_code=language.lower(), **common))
        finally:
            _caches.end()
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
    return {'sample_rate': int(rate), 'samples': int(len(audio)), 'reference_cache': _caches.stats()}


def get_status():
    import importlib.metadata as metadata
    return {'versions': {name: metadata.version(name) for name in ('mlx-audio', 'mlx', 'transformers')},
            'model': _model['path'], 'reference_cache': _caches.stats()}


def main():
    global _PROTOCOL
    _PROTOCOL = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1, encoding='utf-8')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
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
