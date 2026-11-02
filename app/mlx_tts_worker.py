"""Apple MLX Qwen3-TTS worker (#1442). Runs in the separate MLX env, not app/env.

mlx-audio needs transformers 5.x while AA2 pins 4.x, so this file imports
nothing from AA2. Protocol: one JSON object per line on stdin, one reply per
line on stdout. Library chatter is moved to stderr so stdout stays protocol.

Request:  {"id", "op": "generate", "family": "custom"|"clone"|"design"|"lora",
           "model", "text", "language", "speaker", "instruct", "ref_audio",
           "ref_text", "adapter_path", "adapter_generation", "seed", "max_tokens", "output"}
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
        _adapter.update(key=None, originals={}, info=None)
        _caches.clear()
        mx.clear_cache()
        _model['value'] = load_model(path)
        _model['path'] = path
        _caches.attach(_model['value'])
    return _model['value']


LORA_TARGETS = ('q_proj', 'k_proj', 'v_proj', 'o_proj')  # what app/train_lora.py trains
_PEFT_PREFIX, _A_SUFFIX, _B_SUFFIX = 'base_model.model.', '.lora_A.weight', '.lora_B.weight'
_adapter = {'key': None, 'originals': {}, 'info': None}


def get_lora_scale(config):
    """alpha / r for a plain PEFT LoRA on AA2's projections; refuse anything else by name."""
    for flag in ('use_dora', 'use_rslora', 'fan_in_fan_out'):
        if config.get(flag):
            raise ValueError(f'MLX LoRA does not support adapters with {flag}=true; render this voice in local mode.')
    for field in ('bias', 'lora_bias'):
        if config.get(field) not in (None, 'none', False):
            raise ValueError(f'MLX LoRA does not support adapter {field}={config.get(field)!r}.')
    for field in ('modules_to_save', 'rank_pattern', 'alpha_pattern', 'layers_to_transform'):
        if config.get(field):
            raise ValueError(f'MLX LoRA does not support adapter {field}.')
    targets = config.get('target_modules') or []
    targets = [targets] if isinstance(targets, str) else list(targets)
    if not targets or any(name not in LORA_TARGETS for name in targets):
        raise ValueError(f'MLX LoRA supports adapters on {", ".join(LORA_TARGETS)} only; got {targets}.')
    rank, alpha = config.get('r'), config.get('lora_alpha')
    if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0 or not isinstance(alpha, (int, float)) or alpha <= 0:
        raise ValueError(f'Adapter config needs a positive integer r and positive lora_alpha; got r={rank!r}, alpha={alpha!r}.')
    return alpha / rank


def get_lora_targets(keys):
    """PEFT key pairs -> {talker module path: (A key, B key)}; every key must pair up."""
    keys = set(keys)
    targets = {}
    for key in keys:
        if not key.startswith(_PEFT_PREFIX) or not key.endswith((_A_SUFFIX, _B_SUFFIX)):
            raise ValueError(f'Unrecognised adapter tensor {key!r}; expected PEFT LoRA A/B weights.')
        if key.endswith(_A_SUFFIX):
            path = key[len(_PEFT_PREFIX):-len(_A_SUFFIX)]
            if path.rsplit('.', 1)[-1] not in LORA_TARGETS:
                raise ValueError(f'Adapter targets {path!r}; MLX LoRA supports {", ".join(LORA_TARGETS)} only.')
            partner = key[:-len(_A_SUFFIX)] + _B_SUFFIX
            if partner not in keys:
                raise ValueError(f'Adapter tensor {key!r} has no matching lora_B.')
            targets[path] = (key, partner)
    if not targets or len(targets) * 2 != len(keys):
        raise ValueError('Adapter has unpaired or no LoRA tensors.')
    return targets


def _get_module(root, path):
    node = root
    for part in path.split('.'):
        node = node[int(part)] if part.isdigit() else getattr(node, part)
    return node


def _set_module(root, path, module):
    parent_path, _, name = path.rpartition('.')
    parent = _get_module(root, parent_path) if parent_path else root
    if name.isdigit():
        parent[int(name)] = module
    else:
        setattr(parent, name, module)


def ensure_adapter_removed(model):
    """Put the original projections back; Base is unchanged afterwards."""
    for path, original in _adapter['originals'].items():
        _set_module(model.talker, path, original)
    _adapter.update(key=None, originals={}, info=None)


def ensure_adapter(model, adapter_dir, generation):
    """Wrap Base's projections with this adapter, unfused: the bf16 weights are never rewritten.

    A bf16 merge rounds away much of a small LoRA delta (measured on #1442: median 72%
    of changed elements), so the delta stays a separate fp32 path cast to the input dtype.
    """
    import mlx.core as mx
    import mlx.nn as nn

    class LoRALinear(nn.Module):
        def __init__(self, base, lora_a, lora_b, scale):
            super().__init__()
            self.base, self.lora_a, self.lora_b, self.scale = base, lora_a, lora_b, scale

        def __call__(self, x):
            delta = (x.astype(mx.float32) @ self.lora_a.T) @ self.lora_b.T
            return self.base(x) + (self.scale * delta).astype(x.dtype)

    key = (id(model), generation)
    if _adapter['key'] == key:
        return _adapter['info']
    ensure_adapter_removed(model)
    with open(os.path.join(adapter_dir, 'adapter_config.json'), encoding='utf-8') as handle:
        scale = get_lora_scale(json.load(handle))
    weights = mx.load(os.path.join(adapter_dir, 'adapter_model.safetensors'))
    wrapped = {}
    for path, (a_key, b_key) in get_lora_targets(weights).items():
        try:
            base = _get_module(model.talker, path)
        except (AttributeError, IndexError, KeyError) as error:
            raise ValueError(f'Adapter targets {path!r}, which this Base model does not have.') from error
        if type(base) is not nn.Linear:
            raise ValueError(f'Adapter target {path!r} is {type(base).__name__}, not a plain Linear.')
        out_features, in_features = base.weight.shape
        lora_a, lora_b = weights[a_key].astype(mx.float32), weights[b_key].astype(mx.float32)
        if lora_a.ndim != 2 or lora_a.shape[1] != in_features or lora_b.shape != (out_features, lora_a.shape[0]):
            raise ValueError(f'Adapter shapes for {path!r} do not fit the Base model.')
        wrapped[path] = (base, LoRALinear(base, lora_a, lora_b, scale))
    for path, (base, module) in wrapped.items():  # nothing is swapped until every target checked out
        _set_module(model.talker, path, module)
    mx.eval([module.parameters() for _, module in wrapped.values()])
    _adapter.update(key=key, originals={path: base for path, (base, _) in wrapped.items()},
                    info={'generation': generation, 'modules': len(wrapped), 'scale': scale})
    return _adapter['info']


class ensure_instruct:
    """Base generate() drops `instruct`; pass it to the input builder for one request."""

    def __init__(self, model, instruct):
        self.model, self.instruct = model, instruct

    def __enter__(self):
        if self.instruct:
            original = self.model._prepare_generation_inputs

            def with_instruct(*args, **kwargs):
                kwargs.setdefault('instruct', self.instruct)
                return original(*args, **kwargs)
            object.__setattr__(self.model, '_prepare_generation_inputs', with_instruct)
        return self

    def __exit__(self, *exc):
        vars(self.model).pop('_prepare_generation_inputs', None)
        return False


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
    adapter = None
    if family == 'lora':
        adapter = ensure_adapter(model, request['adapter_path'], request['adapter_generation'])
    else:
        ensure_adapter_removed(model)  # clone shares Base; it must never run with a voice adapter
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
    elif family == 'lora':
        # AA2's LoRA serving shape: x-vector-only prompt from ref_sample.wav (no ref_text), plus instruct.
        digest, ref_audio = _caches.prepare(request['ref_audio'], model.sample_rate, load_reference)
        _caches.begin(digest, ref_audio)
        try:
            with ensure_instruct(model, request.get('instruct') or None):
                results = list(model.generate(text=text, ref_audio=ref_audio, lang_code=language.lower(), **common))
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
    reply = {'sample_rate': int(rate), 'samples': int(len(audio)), 'reference_cache': _caches.stats()}
    if adapter is not None:
        reply['adapter'] = adapter
    return reply


def get_status():
    import importlib.metadata as metadata
    return {'versions': {name: metadata.version(name) for name in ('mlx-audio', 'mlx', 'transformers')},
            'model': _model['path'], 'reference_cache': _caches.stats(), 'adapter': _adapter['info']}


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
