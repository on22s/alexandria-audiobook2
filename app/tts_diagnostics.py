"""Bounded, process-wide TTS observations. Caller content never enters records."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import math
import re
import threading
import time

from diagnostics import build_diagnostics
from audio_generation_owner import AudioGenerationCancelled, ExportCancelled

_STAGES = ('model_preparation', 'reference_preparation', 'inference_decode',
           'external_request', 'audio_validation_save')
_FAILURES = frozenset(('unsupported_backend', 'out_of_memory', 'invalid_audio',
                      'missing_asset', 'timeout', 'generation_error', 'missing_configuration'))
_CURRENT = ContextVar('tts_observation', default=None)
_ACTIVE_STAGE = ContextVar('tts_observation_stage', default=None)
_ESCAPED_STAGE = ContextVar('tts_observation_escaped_stage', default=None)
_LOCK = threading.Lock()
_RECORDS = deque(maxlen=50)
_OPERATIONS = frozenset(('generate_custom_voice', 'generate_clone_voice',
    'generate_voice_result', 'generate_voice', 'generate_ensemble_voice',
    'generate_voice_design', 'generate_design_voice', 'generate_lora_voice',
    'generate_batch', 'run_benchmark_batch', 'run_clone_benchmark_batch'))


class _Observation:
    def __init__(self, owner, operation):
        self.owner = owner
        self.operation = operation
        mode = getattr(owner, 'mode', 'unknown')
        self.backend = mode if mode in ('local', 'external', 'mlx') else 'unknown'
        self.lock = threading.Lock()
        self.closed = False
        self.stages = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.reference_cache = None
        self.audio_seconds = 0.0
        self.audio_files = 0
        self.failures = set()

    def apply_stage(self, name, seconds):
        with self.lock:
            if not self.closed:
                self.stages[name] = self.stages.get(name, 0.0) + max(0, seconds)


def capture_tts_operation(operation):
    """Observe a root render without changing results, exceptions or inputs."""
    def decorate(function):
        @wraps(function)
        def run(self, *args, **kwargs):
            parent = _CURRENT.get()
            if parent is not None and parent.owner is self:
                return function(self, *args, **kwargs)
            observation = _Observation(self, operation)
            token = _CURRENT.set(observation)
            stage_token = _ACTIVE_STAGE.set(None)
            escaped_token = _ESCAPED_STAGE.set(None)
            started = time.monotonic()
            outcome = 'interrupted'
            completed = failed = None
            try:
                result = function(self, *args, **kwargs)
                if isinstance(result, bool):
                    outcome = 'completed' if result else 'failed'
                elif hasattr(result, 'success'):
                    outcome = 'completed' if result.success else 'failed'
                elif isinstance(result, dict) and 'completed' in result and 'failed' in result:
                    completed, failed = len(result['completed']), len(result['failed'])
                    outcome = 'partial' if failed and completed else ('failed' if failed else 'completed')
                else:
                    outcome = 'completed'
                return result
            except (AudioGenerationCancelled, ExportCancelled):
                outcome = 'interrupted'
                raise
            except Exception:
                outcome = 'failed'
                raise
            finally:
                _CURRENT.reset(token)
                _ACTIVE_STAGE.reset(stage_token)
                _ESCAPED_STAGE.reset(escaped_token)
                with observation.lock:
                    observation.closed = True
                    failures = sorted(observation.failures, key=lambda item: (item[0], item[1] or '')) if outcome in ('failed', 'partial') else []
                    if outcome in ('failed', 'partial') and not failures:
                        failures = [('generation_error', None)]
                    categories = {category for category, _stage in failures}
                    stages = {stage for _category, stage in failures}
                    record = {
                        'operation': operation, 'backend': observation.backend, 'outcome': outcome,
                        'elapsed_seconds': round(max(0, time.monotonic() - started), 6),
                        'stages_seconds': {name: round(observation.stages[name], 6)
                                          if name in observation.stages else None for name in _STAGES},
                        'reference_cache_hits': observation.cache_hits,
                        'reference_cache_misses': observation.cache_misses,
                        'reference_cache': observation.reference_cache,
                        'audio_seconds': round(observation.audio_seconds, 6) if observation.audio_files else None,
                        'audio_files': observation.audio_files,
                        'completed_items': completed, 'failed_items': failed,
                        'failure_category': next(iter(categories)) if len(categories) == 1 else ('multiple_failures' if categories else None),
                        'failure_stage': next(iter(stages)) if len(stages) == 1 else None,
                        'failure_groups': [{'category': category, 'stage': stage} for category, stage in failures],
                    }
                with _LOCK:
                    _RECORDS.append(record)
        return run
    return decorate


@contextmanager
def measure_tts_stage_time(name):
    if name not in _STAGES:
        raise ValueError('Unknown TTS observation stage')
    observation = _CURRENT.get()
    token = _ACTIVE_STAGE.set(name)
    _ESCAPED_STAGE.set(None)
    started = time.monotonic()
    try:
        yield
    except (AudioGenerationCancelled, ExportCancelled):
        raise
    except Exception as error:
        from tts import get_tts_failure_classification
        escaped = _ESCAPED_STAGE.get()
        if escaped is None or escaped[0] is not error:
            _ESCAPED_STAGE.set((error, name))
        try:
            category, _next_action = get_tts_failure_classification(error)
        except Exception:
            category = 'generation_error'
        record_tts_failure(category, error)
        raise
    finally:
        _ACTIVE_STAGE.reset(token)
        if observation is not None:
            observation.apply_stage(name, time.monotonic() - started)


def measure_tts_stage(name):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with measure_tts_stage_time(name):
                return function(*args, **kwargs)
        return run
    return decorate


def record_tts_cache(hit):
    observation = _CURRENT.get()
    if observation is not None:
        with observation.lock:
            if not observation.closed:
                if hit:
                    observation.cache_hits += 1
                else:
                    observation.cache_misses += 1


def record_tts_reference_cache(prepared, prompts):
    """Observe bounded aggregate counts, never reference paths/text/tensors."""
    observation = _CURRENT.get()
    if observation is None:
        return
    names = ('hits', 'misses', 'evictions', 'bypasses', 'cleared_entries',
             'entries', 'charged_bytes', 'max_entries', 'max_bytes')
    state = {tier: {name: values[name] for name in names
                    if type(values.get(name)) is int and values[name] >= 0}
             for tier, values in (('prepared_audio', prepared), ('clone_prompts', prompts))}
    with observation.lock:
        if not observation.closed:
            observation.reference_cache = state


def record_tts_failure(category, error=None):
    stage = _ACTIVE_STAGE.get()
    escaped = _ESCAPED_STAGE.get()
    if escaped is not None and escaped[0] is error:
        stage = escaped[1]
    observation = _CURRENT.get()
    if observation is not None:
        with observation.lock:
            if not observation.closed:
                observation.failures.add((category if category in _FAILURES else 'generation_error', stage))


def record_tts_audio(path):
    observation = _CURRENT.get()
    if observation is None:
        return
    try:
        import soundfile as sf
        seconds = float(sf.info(path).duration)
        if not math.isfinite(seconds) or seconds <= 0:
            return
    except Exception:
        # Diagnostics must not turn an already successful render into a failure.
        return
    with observation.lock:
        if not observation.closed:
            observation.audio_seconds += seconds
            observation.audio_files += 1


def _get_enum(value, choices):
    return value if type(value) is str and value in choices else None


def _get_number(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value) else None


def _get_token(value, pattern):
    return value if type(value) is str and len(value) <= 100 and re.fullmatch(pattern, value) else None


def _get_mapping(value):
    return value if type(value) is dict else {}


def _get_device(value, *, allow_auto=False):
    if type(value) is not str or len(value) > 64:
        return None
    from device_utils import normalize_device
    try:
        normalized = normalize_device(value, allow_auto=allow_auto)
    except ValueError:
        return None
    return _get_token(normalized, r'(?:auto|cpu|mps|cuda(?::[0-9]{1,2})?)')


def _get_report_record(record):
    source = _get_mapping(record)
    result = {
        'operation': _get_enum(source.get('operation'), _OPERATIONS) or 'unknown',
        'backend': _get_enum(source.get('backend'), ('local', 'external', 'mlx')) or 'unknown',
        'outcome': _get_enum(source.get('outcome'), ('completed', 'failed', 'partial', 'interrupted')) or 'unknown',
        'failure_category': _get_enum(source.get('failure_category'), _FAILURES | {'multiple_failures'}),
        'failure_stage': _get_enum(source.get('failure_stage'), _STAGES),
        'stages_seconds': {name: _get_number(_get_mapping(source.get('stages_seconds')).get(name)) for name in _STAGES},
    }
    for name in ('elapsed_seconds', 'reference_cache_hits', 'reference_cache_misses',
                 'audio_seconds', 'audio_files', 'completed_items', 'failed_items'):
        result[name] = _get_number(source.get(name))
    cache = source.get('reference_cache')
    result['reference_cache'] = None if type(cache) is not dict else {
        tier: {name: _get_number(_get_mapping(cache.get(tier)).get(name)) for name in
               ('hits', 'misses', 'evictions', 'bypasses', 'cleared_entries',
                'entries', 'charged_bytes', 'max_entries', 'max_bytes')}
        for tier in ('prepared_audio', 'clone_prompts')}
    groups = source.get('failure_groups')
    safe_groups = set()
    if type(groups) is list:
        for group in groups[:48]:
            group = _get_mapping(group)
            category = _get_enum(group.get('category'), _FAILURES)
            if category:
                safe_groups.add((category, _get_enum(group.get('stage'), _STAGES)))
    result['failure_groups'] = [{'category': category, 'stage': stage} for category, stage in
                                sorted(safe_groups, key=lambda item: (item[0], item[1] or ''))]
    identity = '|'.join(str(result[name]) for name in
                        ('operation', 'backend', 'outcome', 'failure_category', 'failure_stage'))
    identity += '|' + '|'.join(category + ':' + (stage or 'unobserved') for category, stage in
                              sorted(safe_groups, key=lambda item: (item[0], item[1] or '')))
    result['error_fingerprint'] = ('tts-v1-' + hashlib.sha256(identity.encode('ascii')).hexdigest()[:16]
                                   if result['outcome'] in ('failed', 'partial') else None)
    return result


def _get_version(value):
    if type(value) is not str or len(value) > 100:
        return None
    public, separator, local = value.partition('+')
    public = _get_token(public, r'[0-9]+(?:\.[0-9]+){0,3}(?:(?:a|b|rc)[0-9]+)?(?:\.?(?:post|dev)[0-9]+)?')
    if public and separator and re.fullmatch(r'(?:cpu|cu[0-9]+|rocm[0-9.]+|rocmsdk[0-9]+|xpu)', local):
        return public + '+' + local
    return public


def _get_report_runtime(runtime):
    source = _get_mapping(runtime)
    platform = _get_mapping(source.get('platform'))
    packages = _get_mapping(source.get('packages'))
    from runtime_info import RUNTIME_PACKAGES
    revision = _get_token(source.get('revision'), r'(?:[a-fA-F0-9]{7,64}|release-[0-9]{4}-[0-9]{2}|v?[0-9]+(?:\.[0-9]+){1,3})')
    release = platform.get('release')
    release_match = re.match(r'[0-9]+(?:\.[0-9]+){0,3}', release) if type(release) is str and len(release) <= 100 else None
    return {
        'revision': revision,
        'revision_source': _get_enum(source.get('revision_source'), ('git', 'environment')) if revision else 'unavailable',
        'revision_scope': _get_enum(source.get('revision_scope'), ('checkout', 'build')) if revision else None,
        'dirty': source.get('dirty') if type(source.get('dirty')) is bool else None,
        'identity_note': 'Checkout/build observation only; loaded modules may predate it. Null means unavailable or excluded.',
        'python': _get_version(source.get('python')),
        'platform': {'system': _get_enum(platform.get('system'), ('Windows', 'Linux', 'Darwin')),
                     'machine': _get_enum(platform.get('machine'), ('AMD64', 'x86_64', 'ARM64', 'arm64', 'aarch64', 'armv7l', 'ppc64le', 'riscv64', 'i386', 'i686')),
                     'release': release_match.group() if release_match else None},
        'version_note': 'Numeric OS release and public dependency versions; private build/local suffixes are omitted.',
        'packages': {name: _get_version(packages.get(name)) for name in RUNTIME_PACKAGES},
    }


def _get_report_residency(residency):
    source = _get_mapping(residency)
    if source.get('state') == 'busy':
        return {'state': 'busy', 'active_leases': None, 'residents': None}
    residents = source.get('residents')
    result = {
        'state': _get_enum(source.get('state'), ('busy', 'not_initialized', 'observed', 'unavailable')) or 'unavailable',
        'mode': _get_enum(source.get('mode'), ('local', 'external', 'mlx')),
        'configured_device': _get_device(source.get('configured_device'), allow_auto=True),
        'active_leases': _get_number(source.get('active_leases')),
        'cleanup_pending': source.get('cleanup_pending') if type(source.get('cleanup_pending')) is bool else None,
        'identity_note': 'Mode/device settings are configuration, not loaded truth. Resident device is observed; dtype is unobserved.',
        'residents': None,
    }
    if type(residents) is list:
        result['residents'] = []
        for model in residents[:4]:
            model = _get_mapping(model)
            capture = _get_mapping(model.get('load_capture'))
            result['residents'].append({
                'family': _get_enum(model.get('family'), ('custom', 'clone', 'design', 'lora')),
                'device': _get_device(model.get('device')),
                'dtype': None,
                'load_capture': ({'scope': 'captured_at_load_not_revalidated'}
                    if capture.get('scope') == 'captured_at_load_not_revalidated' else None),
            })
    return result


def get_tts_diagnostics_bundle(*, residency=None, runtime=None, assets=None, device=None, configuration=None, collection=None):
    with _LOCK:
        snapshot = list(_RECORDS)
    records = []
    record_bytes = 0
    for source in reversed(snapshot):
        record = _get_report_record(source)
        size = len(json.dumps(record, ensure_ascii=True).encode('utf-8'))
        if record_bytes + size > 48000:
            break
        records.append(record)
        record_bytes += size
    records.reverse()
    safe_assets = []
    for asset in (assets[:3] if type(assets) is list else []):
        asset = _get_mapping(asset)
        model_id = _get_enum(asset.get('model_id'), (
            'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', 'Qwen/Qwen3-TTS-12Hz-1.7B-Base',
            'Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign'))
        if model_id:
            safe_assets.append({'model_id': model_id, 'state': _get_enum(asset.get('state'),
                ('required_assets_observed', 'not_cached', 'unavailable')) or 'unavailable'})
    device = _get_mapping(device)
    configuration = _get_mapping(configuration)
    bundle = build_diagnostics({'tts': {
        'scope': 'Recent operations in this app process, across books',
        'timing_note': 'Nested and concurrent stage times overlap; do not sum them. Inference includes decoding. Null means unavailable.',
        'records': records,
        'records_omitted': len(snapshot) - len(records),
        'configuration': {'mode': _get_enum(configuration.get('mode'), ('local', 'external', 'mlx')),
                          'device': _get_device(configuration.get('device'), allow_auto=True),
                          'scope': 'configuration_with_defaults_not_loaded_state'},
        'residency': _get_report_residency(residency),
        'runtime': _get_report_runtime(runtime),
        'assets': safe_assets,
        'device': {'state': _get_enum(device.get('state'), ('observed', 'runtime_not_loaded', 'unavailable')) or 'unavailable',
                   'gpu_backend': _get_enum(device.get('gpu_backend'), ('cuda', 'mps'))},
        'collection': {name: _get_enum(_get_mapping(collection).get(name), ('collected', 'unavailable')) or 'not_requested'
                       for name in ('residency', 'runtime', 'assets', 'device', 'configuration')},
        'collection_note': 'Collected means the observer returned, not that optional fields are available or validated.',
    }}, schema_version=2, note='Allowlisted local report. No raw errors, logs, text, paths or endpoints. Sharing is separate. Null means unobserved or excluded. Private troubleshooting logs remain separate; never publish them without review.')
    return bundle


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
