"""Actual loaded CustomVoice dependency binding under existing local serialization.

This is a declared known Python/native build contract, not attestation of hostile
in-memory tensor mutation. No disk source hashes relabel imported functions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import importlib
import json
import os
from pathlib import Path
import platform
import sys
from types import CodeType, FunctionType, MethodType

from synthesis_recovery_identity import RecoveryIdentityUnavailable, get_recovery_digest
from synthesis_recovery_native import (NativeRecoveryLoadedModel, get_native_custom_code_sha256,
                                       _QWEN_CODE_MANIFEST)

MAX_SERVING_MODULES = 2048
MAX_TOKENIZER_BYTES = 16 * 1024 * 1024
_ACTIVE_GUARD = ContextVar('native_recovery_serving_guard', default=None)
_AA2_MODULES = (
    'tts', 'project', 'utils', 'audio_generation_owner', 'audio_validation',
    'speech_text', 'pronunciation', 'synthesis_settings', 'synthesis_windows', 'synthesis_input',
    'synthesis_audio', 'synthesis_render', 'synthesis_workspace', 'synthesis_recovery_cache',
    'synthesis_recovery_identity', 'synthesis_recovery_context', 'synthesis_recovery_code',
    'loaded_module_code', 'synthesis_recovery_native', 'synthesis_recovery_serving', 'device_utils', 'tts_diagnostics',
    'synthesis_recovery', 'clone_reference_cache', 'take_publication',
)
_MODULE_STATE_FIELDS = (
    'training', 'eps', 'variance_epsilon', 'p', 'inplace', 'stride', 'padding',
    'dilation', 'groups', 'kernel_size', 'output_padding', 'transposed', 'padding_mode',
    'scaling', 'attention_dropout', 'is_causal', 'hidden_size', 'num_heads',
    'head_dim', 'num_key_value_groups', 'num_key_value_heads',
)
_UNSUPPORTED_GENERATION_VALUES = {
    'num_beams': (None, 1), 'num_beam_groups': (None, 1), 'num_return_sequences': (None, 1),
    'penalty_alpha': (None,), 'constraints': (None,), 'force_words_ids': (None,),
    'dola_layers': (None,), 'custom_generate': (None,), 'guidance_scale': (None, 1.0),
    'watermarking_config': (None,), 'assistant_early_exit': (None,),
    'prompt_lookup_num_tokens': (None,), 'is_assistant': (None, False),
    'sequence_bias': (None,), 'bad_words_ids': (None,), 'forced_bos_token_id': (None,),
    'forced_eos_token_id': (None,), 'exponential_decay_length_penalty': (None,),
    'remove_invalid_values': (None, False), 'renormalize_logits': (None, False),
    'typical_p': (None, 1.0), 'epsilon_cutoff': (None, 0.0), 'eta_cutoff': (None, 0.0),
    'min_p': (None,), 'top_h': (None,), 'encoder_repetition_penalty': (None, 1.0),
    'encoder_no_repeat_ngram_size': (None, 0), 'no_repeat_ngram_size': (None, 0),
    'max_time': (None,), 'stop_strings': (None,),
}


class SynthesisRecoveryDependencyChanged(RuntimeError):
    """An admitted parent cannot mix different/unproven serving dependencies."""


@dataclass(frozen=True)
class NativeRecoveryServingIdentity:
    sha256: str
    asset_sha256: str
    loader_code_sha256: str


def _get_declared_class(module_name, name):
    return getattr(importlib.import_module(module_name), name)


def _ensure_class(value, module_name, name):
    if type(value) is not _get_declared_class(module_name, name):
        raise RecoveryIdentityUnavailable('unknown_serving_class')
    return value


def _get_native_method(value, name):
    if name in vars(value):
        raise RecoveryIdentityUnavailable('overridden_native_method')
    callback = getattr(value, name)
    expected = getattr(type(value), name)
    if (type(callback) is not MethodType or callback.__self__ is not value
            or callback.__func__ is not expected):
        raise RecoveryIdentityUnavailable('unqualified_native_method')
    return callback


def _get_config_data(config):
    # Known config serializers are part of the declared library-build boundary.
    # Do not persist paths/commit hints or unstable object hashes as semantics.
    data = config.to_dict()
    nodes = [0]
    def copy_value(value, depth=0):
        nodes[0] += 1
        if depth > 32 or nodes[0] > 8192:
            raise RecoveryIdentityUnavailable('serving_config_limit')
        if type(value) is dict:
            if len(value) > 2048:
                raise RecoveryIdentityUnavailable('serving_config_limit')
            return {key: copy_value(item, depth + 1) for key, item in value.items()
                    if key not in ('_name_or_path', '_commit_hash', '_original_object_hash', 'transformers_version')}
        if type(value) in (tuple, list):
            if len(value) > 2048:
                raise RecoveryIdentityUnavailable('serving_config_limit')
            return [copy_value(item, depth + 1) for item in value]
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise RecoveryIdentityUnavailable('unsupported_serving_config_value')
    return copy_value(data)


def get_native_tokenizer_semantic_sha256(tokenizer):
    if not 0 < len(tokenizer) <= 200000:
        raise RecoveryIdentityUnavailable('tokenizer_vocabulary_limit')
    serialized = tokenizer.backend_tokenizer.to_str()
    if type(serialized) is not str or len(serialized) > MAX_TOKENIZER_BYTES:
        raise RecoveryIdentityUnavailable('tokenizer_identity_limit')
    data = json.loads(serialized)
    if not isinstance(data, dict):
        raise RecoveryIdentityUnavailable('unsupported_tokenizer_identity')
    # These are transient settings overwritten by every known processor call,
    # not a different vocabulary/normalizer/postprocessor. Bind the actual
    # public padding/truncation policy separately, including model_max_length.
    data.pop('padding', None)
    data.pop('truncation', None)
    return get_recovery_digest(data)


def _get_generation_state(value):
    config = _get_config_data(value.generation_config)
    for key, supported in _UNSUPPORTED_GENERATION_VALUES.items():
        if config.get(key) not in supported:
            raise RecoveryIdentityUnavailable('unsupported_generation_mode')
    return config


def _get_processor_call_kwargs(processor, tokenizer):
    # Match Qwen3TTSModel._tokenize_texts, including the processor's effective
    # defaults from init_kwargs. Loader-only paths do not enter this policy.
    module = importlib.import_module('qwen_tts.core.models.processing_qwen3_tts')
    return processor._merge_kwargs(module.Qwen3TTSProcessorKwargs,
        tokenizer_init_kwargs=tokenizer.init_kwargs, return_tensors='pt', padding=True)


def _get_aa2_loaded_code():
    result = {}
    for name in _AA2_MODULES:
        module = importlib.import_module(name)
        digest = getattr(module, '_LOADED_RECOVERY_CODE_SHA256', None)
        if not isinstance(digest, str) or len(digest) != 64:
            raise RecoveryIdentityUnavailable('missing_loaded_aa2_code')
        result[name] = digest
    return result


def _capture_engine_methods(engine, callbacks, code_files):
    import tts
    import device_utils
    import gpu_stats
    references = getattr(tts, '_RAW_RECOVERY_METHODS', None)
    if type(engine) is not tts.TTSEngine or not isinstance(references, dict):
        raise RecoveryIdentityUnavailable('unqualified_native_engine')
    if (type(engine._LOCAL_MODEL_IDS) is not dict or engine._LOCAL_MODEL_IDS != tts._RAW_RECOVERY_MODEL_IDS
            or device_utils.is_allocation_failure is not tts._RAW_RECOVERY_ALLOCATION_FAILURE
            or gpu_stats.is_allocation_failure is not tts._RAW_RECOVERY_ALLOCATION_FAILURE
            or gpu_stats.OOM_MARKERS != tts._RAW_RECOVERY_ALLOCATION_MARKERS):
        raise RecoveryIdentityUnavailable('overridden_model_load_policy')
    callbacks['actual.allocation_failure'] = device_utils.is_allocation_failure
    code_files.append(gpu_stats.__file__)
    for name, expected in references.items():
        callback = getattr(engine, name)
        function = callback.__func__ if type(callback) is MethodType else callback
        if function is not expected:
            raise RecoveryIdentityUnavailable('overridden_engine_method')
        callbacks['actual.engine.' + name] = callback
    for name, expected in tts._RAW_RECOVERY_FUNCTIONS.items():
        if getattr(tts, name) is not expected:
            raise RecoveryIdentityUnavailable('overridden_engine_function')
        callbacks['actual.aa2.' + name] = expected
    for name in ('tts', 'tts_diagnostics', 'device_utils'):
        code_files.append(importlib.import_module(name).__file__)


def _capture_serving_modules(root, prefix, callbacks, roles):
    state = []
    for index, (name, module) in enumerate(root.named_modules()):
        if index >= MAX_SERVING_MODULES:
            raise RecoveryIdentityUnavailable('serving_module_limit')
        cls = type(module)
        if any(getattr(module, field, None) for field in
               ('_forward_hooks', '_forward_pre_hooks', '_backward_hooks', '_backward_pre_hooks')):
            raise RecoveryIdentityUnavailable('unsupported_serving_hooks')
        if (getattr(module, '_hf_hook', None) is not None
                or getattr(module, '_compiled_call_impl', None) is not None
                or 'forward' in vars(module)):
            raise RecoveryIdentityUnavailable('overridden_serving_module')
        if cls.__module__ in _QWEN_CODE_MANIFEST:
            declared = _QWEN_CODE_MANIFEST[cls.__module__]['classes']
            if not isinstance(declared, dict) or cls.__name__ not in declared:
                raise RecoveryIdentityUnavailable('unqualified_serving_module')
        elif not (cls.__module__.startswith('torch.nn.modules.') or cls.__module__ == 'transformers.activations'):
            raise RecoveryIdentityUnavailable('unknown_serving_module')
        if cls is not _get_declared_class(cls.__module__, cls.__name__):
            raise RecoveryIdentityUnavailable('replaced_serving_class')
        row = {'name': name, 'class': cls.__module__ + '.' + cls.__name__}
        for field in _MODULE_STATE_FIELDS:
            if hasattr(module, field):
                row[field] = getattr(module, field)
        config = getattr(module, 'config', None)
        if config is not None:
            selected = getattr(config, '_attn_implementation', None)
            row['attention'] = selected
            if selected not in (None, 'eager', 'sdpa'):
                raise RecoveryIdentityUnavailable('unsupported_attention_backend')
            if 'Attention' in cls.__name__ and selected == 'sdpa':
                registry = getattr(importlib.import_module(cls.__module__), 'ALL_ATTENTION_FUNCTIONS')
                callbacks['actual.' + prefix + '.' + name + '.attention'] = registry[selected]
        if hasattr(module, 'rope_init_fn'):
            rope = getattr(module, 'rope_type', 'default')
            if rope != 'default':
                raise RecoveryIdentityUnavailable('unsupported_rotary_cache_contract')
            callbacks['actual.' + prefix + '.' + name + '.rope'] = module.rope_init_fn
        if cls.__module__ == 'transformers.activations':
            roles.append((module, prefix + '.' + name))
            callbacks['actual.' + prefix + '.' + name + '.forward'] = _get_native_method(module, 'forward')
        state.append(row)
    return state


def _get_runtime_build(torch, device):
    import numpy as np
    import soundfile as sf
    import tokenizers
    build = {
        'python': sys.version, 'implementation': platform.python_implementation(),
        'machine': platform.machine(), 'kernel': platform.release(),
        'torch': str(torch.__version__), 'torch_git': torch.version.git_version,
        'cuda_build': torch.version.cuda, 'hip_build': torch.version.hip,
        'transformers': version('transformers'), 'qwen_tts': version('qwen-tts'),
        'tokenizers': tokenizers.__version__, 'numpy': np.__version__,
        'soundfile': sf.__version__, 'libsndfile': sf.__libsndfile_version__,
        'device': str(device), 'torch_config_sha256': get_recovery_digest(torch.__config__.show()),
        'threads': torch.get_num_threads(), 'interop_threads': torch.get_num_interop_threads(),
        'deterministic': torch.are_deterministic_algorithms_enabled(),
        'deterministic_warn_only': torch.is_deterministic_algorithms_warn_only_enabled(),
        'float32_matmul_precision': torch.get_float32_matmul_precision(),
        'grad_enabled': torch.is_grad_enabled(), 'inference_mode': torch.is_inference_mode_enabled(),
        'cudnn': {name: getattr(torch.backends.cudnn, name)
                  for name in ('enabled', 'benchmark', 'deterministic', 'allow_tf32')},
        'matmul': {name: getattr(torch.backends.cuda.matmul, name)
                   for name in ('allow_tf32', 'allow_fp16_reduced_precision_reduction',
                                'allow_bf16_reduced_precision_reduction')},
        'sdp': {name: getattr(torch.backends.cuda, name)() for name in
                ('flash_sdp_enabled', 'mem_efficient_sdp_enabled', 'math_sdp_enabled')},
        'environment': {name: os.environ.get(name) for name in (
            'MIOPEN_FIND_MODE', 'FLASH_ATTENTION_TRITON_AMD_ENABLE', 'CUBLAS_WORKSPACE_CONFIG',
            'NVIDIA_TF32_OVERRIDE', 'HIPBLASLT_TENSILE_LIBPATH', 'PYTORCH_ROCM_ARCH')},
    }
    # Bind the loaded native build metadata and actual Linux GPU driver source
    # contract. Missing driver evidence stays temporary; do not invent a build.
    if device.type == 'cuda':
        props = torch.cuda.get_device_properties(device)
        build['gpu'] = {'name': props.name, 'major': props.major, 'minor': props.minor,
                        'gcn_arch': getattr(props, 'gcnArchName', None)}
        paths = (('/sys/module/amdgpu/srcversion', '/sys/module/amdgpu/version')
                 if torch.version.hip else ('/sys/module/nvidia/srcversion', '/proc/driver/nvidia/version'))
        driver = []
        for path in paths:
            try:
                with open(path, 'rb') as handle:
                    value = handle.read(65537)
            except FileNotFoundError:
                continue
            if not value or len(value) > 65536:
                raise RecoveryIdentityUnavailable('unsupported_driver_identity')
            driver.append({'source': Path(path).name, 'sha256': hashlib.sha256(value).hexdigest()})
        if not driver:
            raise RecoveryIdentityUnavailable('missing_driver_identity')
        build['driver'] = driver
    elif device.type != 'cpu':
        raise RecoveryIdentityUnavailable('unsupported_native_recovery_device')
    return build


def _capture_loaded_serving(engine, request, model):
    import tts
    import torch
    loaded = getattr(model, '_aa2_recovery_loaded_model', None)
    if type(loaded) is not NativeRecoveryLoadedModel:
        raise RecoveryIdentityUnavailable('model_loaded_without_identity')
    if get_native_custom_code_sha256() != loaded.loader_code_sha256:
        raise RecoveryIdentityUnavailable('loaded_native_code_changed')
    _ensure_class(model, 'qwen_tts.inference.qwen3_tts_model', 'Qwen3TTSModel')
    root = _ensure_class(model.model, 'qwen_tts.core.models.modeling_qwen3_tts', 'Qwen3TTSForConditionalGeneration')
    talker = _ensure_class(root.talker, 'qwen_tts.core.models.modeling_qwen3_tts', 'Qwen3TTSTalkerForConditionalGeneration')
    _ensure_class(talker.model, 'qwen_tts.core.models.modeling_qwen3_tts', 'Qwen3TTSTalkerModel')
    predictor = _ensure_class(talker.code_predictor, 'qwen_tts.core.models.modeling_qwen3_tts',
                              'Qwen3TTSTalkerCodePredictorModelForConditionalGeneration')
    _ensure_class(predictor.model, 'qwen_tts.core.models.modeling_qwen3_tts', 'Qwen3TTSTalkerCodePredictorModel')
    processor = _ensure_class(model.processor, 'qwen_tts.core.models.processing_qwen3_tts', 'Qwen3TTSProcessor')
    tokenizer = _ensure_class(processor.tokenizer, 'transformers.models.qwen2.tokenization_qwen2_fast', 'Qwen2TokenizerFast')
    codec = _ensure_class(root.speech_tokenizer, 'qwen_tts.inference.qwen3_tts_tokenizer', 'Qwen3TTSTokenizer')
    codec_model = _ensure_class(codec.model, 'qwen_tts.core.tokenizer_12hz.modeling_qwen3_tts_tokenizer_v2',
                                'Qwen3TTSTokenizerV2Model')
    decoder = _ensure_class(codec_model.decoder, 'qwen_tts.core.tokenizer_12hz.modeling_qwen3_tts_tokenizer_v2',
                            'Qwen3TTSTokenizerV2Decoder')
    if root.tts_model_type != 'custom_voice' or root.tokenizer_type != 'qwen3_tts_tokenizer_12hz':
        raise RecoveryIdentityUnavailable('unsupported_native_model_kind')
    callbacks, code_files = {}, []
    _capture_engine_methods(engine, callbacks, code_files)
    roles = [(engine, 'engine'), (model, 'wrapper'), (root, 'model'), (talker, 'talker'),
             (predictor, 'predictor'), (processor, 'processor'), (tokenizer, 'tokenizer'),
             (codec, 'codec_wrapper'), (codec_model, 'codec_model'), (decoder, 'decoder')]
    for value, role in roles[1:]:
        for name in ({'wrapper': ('generate_custom_voice', '_merge_generate_kwargs'),
                      'model': ('generate',), 'talker': ('generate',), 'predictor': ('generate',),
                      'processor': ('__call__', '_merge_kwargs'), 'tokenizer': ('__call__', '_batch_encode_plus', '_encode_plus'),
                      'codec_wrapper': ('decode',), 'codec_model': ('decode',), 'decoder': ('forward',)}[role]):
            callbacks['actual.' + role + '.' + name] = _get_native_method(value, name)
    wrapped = decoder.chunked_decode
    factory = tts._RAW_RECOVERY_FUNCTIONS['get_memory_bounded_codec_decode']
    inner_codes = [value for value in factory.__code__.co_consts if type(value) is CodeType
                   and value.co_name == 'decode_with_recovery']
    original = getattr(wrapped, '__wrapped__', None)
    if (type(wrapped) is not FunctionType or len(inner_codes) != 1 or wrapped.__code__ is not inner_codes[0]
            or type(original) is not MethodType or original.__self__ is not decoder
            or original.__func__ is not type(decoder).chunked_decode
            or wrapped.__code__.co_freevars != ('decode',)
            or wrapped.__closure__[0].cell_contents is not original):
        raise RecoveryIdentityUnavailable('unknown_codec_recovery_wrapper')
    callbacks['actual.decoder.chunked_decode'] = wrapped
    # Actual wrappers still own their declared hooks/config. Global Torch module
    # hooks would change every forward and remain outside this first contract.
    torch_module = importlib.import_module('torch.nn.modules.module')
    if any(getattr(torch_module, name, None) for name in (
            '_global_forward_hooks', '_global_forward_pre_hooks', '_global_backward_hooks',
            '_global_backward_pre_hooks')):
        raise RecoveryIdentityUnavailable('unsupported_global_serving_hooks')
    modules = {'talker': _capture_serving_modules(talker, 'talker', callbacks, roles),
               'decoder': _capture_serving_modules(decoder, 'decoder', callbacks, roles)}
    for value in (root, codec_model):
        if any(getattr(value, name, None) for name in ('_forward_hooks', '_forward_pre_hooks', '_backward_hooks')):
            raise RecoveryIdentityUnavailable('unsupported_serving_hooks')
    sampler = model._merge_generate_kwargs(max_new_tokens=engine._max_new_tokens)
    configs = {'model': _get_config_data(root.config), 'talker': _get_config_data(talker.config),
               'predictor': _get_config_data(predictor.config), 'codec': _get_config_data(codec_model.config),
               'decoder': _get_config_data(decoder.config)}
    generations = {name: _get_generation_state(value) for name, value in (
        ('model', root), ('talker', talker), ('predictor', predictor))}
    dtype_names = ((torch.float16, 'float16'), (torch.bfloat16, 'bfloat16'),
                   (torch.float32, 'float32'), (torch.float64, 'float64'))
    dtype = next((name for value, name in dtype_names if root.dtype is value), None)
    default_dtype = next((name for value, name in dtype_names if torch.get_default_dtype() is value), None)
    if dtype is None or default_dtype is None or root.device != model.device:
        raise RecoveryIdentityUnavailable('unqualified_serving_precision')
    state = {
        'asset_sha256': loaded.asset_sha256, 'load_options_sha256': loaded.options_sha256,
        'aa2_loaded_code': _get_aa2_loaded_code(),
        'runtime': _get_runtime_build(torch, root.device), 'dtype': dtype, 'default_dtype': default_dtype,
        'language': engine._language, 'max_new_tokens': engine._max_new_tokens,
        'voice': request.voice, 'seed': request.seed, 'instruction_sha256': get_recovery_digest(request.instruct),
        'non_streaming_mode': True, 'configs': configs, 'generation': generations,
        'generate_defaults': model.generate_defaults, 'merged_sampler': sampler,
        'modules': modules, 'tokenizer': {
            'backend_sha256': get_native_tokenizer_semantic_sha256(tokenizer),
            'processor_call_kwargs': _get_processor_call_kwargs(processor, tokenizer),
            'padding_side': tokenizer.padding_side, 'truncation_side': tokenizer.truncation_side,
            'model_max_length': tokenizer.model_max_length, 'special_tokens': tokenizer.special_tokens_map},
    }
    digest = get_native_custom_code_sha256(extra_callables=callbacks, extra_code_files=code_files,
                                           bound_roles=roles, state=state)
    return NativeRecoveryServingIdentity(digest, loaded.asset_sha256, loaded.loader_code_sha256)


def capture_native_custom_serving_identity(engine, request):
    """Capture actual serving dependencies atomically with respect to providers."""
    import tts
    with tts._LOCAL_TTS_LOCK:
        if (type(request) is not tts.CustomVoiceRequest or type(request.seed) is not int
                or not 0 <= request.seed < 2**64 or type(request.voice) is not str
                or type(request.instruct) is not str or len(request.voice) > 256
                or len(request.instruct) > 65536 or type(engine._language) is not str
                or len(engine._language) > 128 or engine.get_voice_backend('custom') != 'local'
                or engine._compile_codec_enabled):
            raise RecoveryIdentityUnavailable('unsupported_native_recovery_request')
        # Loader/ownership exceptions are not optional inspection failures.
        model = engine._init_local_custom()
        try:
            return _capture_loaded_serving(engine, request, model)
        except (ImportError, AttributeError, KeyError, ValueError, TypeError, OSError) as error:
            raise RecoveryIdentityUnavailable('unavailable_serving_contract') from error


@contextmanager
def ensure_native_recovery_serving(engine, identity):
    if type(identity) is not NativeRecoveryServingIdentity:
        raise RecoveryIdentityUnavailable('missing_serving_identity')
    token = _ACTIVE_GUARD.set((engine, identity.sha256))
    try:
        yield
    finally:
        _ACTIVE_GUARD.reset(token)


def ensure_native_recovery_serving_current(engine, request):
    """Called under local serialization immediately before missing-window inference."""
    guard = _ACTIVE_GUARD.get()
    if guard is None:
        return
    if guard[0] is not engine:
        raise SynthesisRecoveryDependencyChanged('Recovery serving engine changed')
    try:
        current = capture_native_custom_serving_identity(engine, request)
    except RecoveryIdentityUnavailable as error:
        raise SynthesisRecoveryDependencyChanged('Recovery serving identity became unproven') from error
    if current.sha256 != guard[1]:
        raise SynthesisRecoveryDependencyChanged('Recovery serving dependencies changed')


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
