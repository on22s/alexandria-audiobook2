"""Bounded, process-wide TTS observations. Caller content never enters records."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
import threading
import time

from diagnostics import build_diagnostics

_STAGES = ('model_preparation', 'reference_preparation', 'inference_decode',
           'external_request', 'audio_validation_save')
_FAILURES = frozenset(('unsupported_backend', 'out_of_memory', 'invalid_audio',
                      'missing_asset', 'timeout', 'generation_error', 'missing_configuration'))
_CURRENT = ContextVar('tts_observation', default=None)
_LOCK = threading.Lock()
_RECORDS = deque(maxlen=50)


class _Observation:
    def __init__(self, owner, operation):
        self.owner = owner
        self.operation = operation
        mode = getattr(owner, 'mode', 'unknown')
        self.backend = mode if mode in ('local', 'external') else 'unknown'
        self.lock = threading.Lock()
        self.closed = False
        self.stages = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.reference_cache = None
        self.audio_seconds = 0.0
        self.audio_files = 0
        self.failure = None

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
            except Exception:
                outcome = 'failed'
                raise
            finally:
                _CURRENT.reset(token)
                with observation.lock:
                    observation.closed = True
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
                        'failure_category': observation.failure or ('generation_error' if outcome == 'failed' else None),
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
    started = time.monotonic()
    try:
        yield
    finally:
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


def record_tts_failure(category):
    observation = _CURRENT.get()
    if observation is not None:
        with observation.lock:
            if not observation.closed:
                observation.failure = category if category in _FAILURES else 'generation_error'


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


def get_tts_diagnostics_bundle():
    with _LOCK:
        records = list(_RECORDS)
    return build_diagnostics({'tts': {
        'scope': 'Recent operations in this app process, across books',
        'timing_note': 'Nested and concurrent stage times overlap; do not sum them. Inference includes decoding. Null means unavailable.',
        'records': records,
    }})


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
