"""Explicit known CustomVoice Python serving-code compatibility boundary.

Imports of optional native packages occur only during opt-in eligibility checks.
The manifest is finite; unsupported modes, objects and wrappers stay temporary.
"""
import importlib
from importlib.metadata import version, PackageNotFoundError
from types import FunctionType, MethodType

from synthesis_recovery_identity import RecoveryIdentityUnavailable
from synthesis_recovery_code import get_live_callable_sha256


_QWEN_CODE_MANIFEST = {'qwen_tts.inference.qwen3_tts_model': {'classes': {'Qwen3TTSModel': ['__init__',
                                                                      'from_pretrained',
                                                                      '_supported_languages_set',
                                                                      '_supported_speakers_set',
                                                                      '_validate_languages',
                                                                      '_validate_speakers',
                                                                      '_ensure_list',
                                                                      '_build_assistant_text',
                                                                      '_build_instruct_text',
                                                                      '_tokenize_texts',
                                                                      '_merge_generate_kwargs',
                                                                      'generate_custom_voice']},
                                        'functions': []},
 'qwen_tts.core.models.modeling_qwen3_tts': {'classes': {'Qwen3TTSPreTrainedModel': None,
                                                         'Qwen3TTSTalkerTextPreTrainedModel': None,
                                                         'Qwen3TTSTalkerRotaryEmbedding': None,
                                                         'Qwen3TTSRotaryEmbedding': None,
                                                         'Qwen3TTSRMSNorm': None,
                                                         'Qwen3TTSTalkerAttention': None,
                                                         'Qwen3TTSTalkerResizeMLP': None,
                                                         'Qwen3TTSTalkerTextMLP': None,
                                                         'Qwen3TTSAttention': None,
                                                         'Qwen3TTSDecoderLayer': None,
                                                         'Qwen3TTSTalkerCodePredictorModel': None,
                                                         'Qwen3TTSTalkerCodePredictorModelForConditionalGeneration': None,
                                                         'Qwen3TTSTalkerDecoderLayer': None,
                                                         'Qwen3TTSTalkerModel': None,
                                                         'Qwen3TTSTalkerForConditionalGeneration': None,
                                                         'Qwen3TTSForConditionalGeneration': None},
                                             'functions': ['rotate_half',
                                                           'repeat_kv',
                                                           'eager_attention_forward',
                                                           'apply_multimodal_rotary_pos_emb',
                                                           'apply_rotary_pos_emb']},
 'qwen_tts.core.models.processing_qwen3_tts': {'classes': {'Qwen3TTSProcessor': ['__init__',
                                                                                 '__call__']},
                                               'functions': []},
 'qwen_tts.inference.qwen3_tts_tokenizer': {'classes': {'Qwen3TTSTokenizer': ['__init__',
                                                                              'from_pretrained',
                                                                              'decode',
                                                                              'get_model_type',
                                                                              'get_output_sample_rate']},
                                            'functions': []},
 'qwen_tts.core.tokenizer_12hz.modeling_qwen3_tts_tokenizer_v2': {'classes': {'Qwen3TTSTokenizerV2DecoderPreTrainedModel': None,
                                                                              'Qwen3TTSTokenizerV2CausalConvNet': None,
                                                                              'Qwen3TTSTokenizerV2CausalTransConvNet': None,
                                                                              'Qwen3TTSTokenizerV2ConvNeXtBlock': None,
                                                                              'Qwen3TTSTokenizerV2DecoderRotatoryEmbedding': None,
                                                                              'Qwen3TTSTokenizerV2DecoderAttention': None,
                                                                              'Qwen3TTSTokenizerV2DecoderMlp': None,
                                                                              'Qwen3TTSTokenizerV2DecoderRMSNorm': None,
                                                                              'Qwen3TTSTokenizerV2DecoderLayerScale': None,
                                                                              'Qwen3TTSTokenizerV2DecoderTransformerLayer': None,
                                                                              'Qwen3TTSTokenizerV2DecoderTransformerModel': None,
                                                                              'SnakeBeta': None,
                                                                              'Qwen3TTSTokenizerV2DecoderDecoderResidualUnit': None,
                                                                              'Qwen3TTSTokenizerV2DecoderDecoderBlock': None,
                                                                              'EuclideanCodebook': None,
                                                                              'VectorQuantization': None,
                                                                              'ResidualVectorQuantization': None,
                                                                              'ResidualVectorQuantizer': None,
                                                                              'SplitResidualVectorQuantizer': None,
                                                                              'Qwen3TTSTokenizerV2Decoder': None,
                                                                              'Qwen3TTSTokenizerV2PreTrainedModel': None,
                                                                              'Qwen3TTSTokenizerV2Model': None},
                                                                  'functions': ['rotate_half',
                                                                                'apply_rotary_pos_emb',
                                                                                'repeat_kv',
                                                                                'eager_attention_forward']},
 'qwen_tts.core.models.configuration_qwen3_tts': {'classes': 'all_declared_configs',
                                                  'functions': []},
 'qwen_tts.core.tokenizer_12hz.configuration_qwen3_tts_tokenizer_v2': {'classes': 'all_declared_configs',
                                                                       'functions': []}}

_TRANSFORMERS_CLASS_METHODS = {
    'transformers.generation.utils': {'GenerationMixin': (
        'generate', '_sample', 'prepare_inputs_for_generation', '_prepare_generation_config',
        '_prepare_model_inputs', '_get_logits_processor', '_get_stopping_criteria',
        '_prepare_cache_for_generation', '_expand_inputs_for_generation',
        '_update_model_kwargs_for_generation')},
    'transformers.generation.logits_process': {
        name: ('__init__', '__call__') for name in (
            'TemperatureLogitsWarper', 'TopKLogitsWarper', 'TopPLogitsWarper',
            'RepetitionPenaltyLogitsProcessor', 'MinNewTokensLengthLogitsProcessor',
            'SuppressTokensLogitsProcessor')},
    'transformers.generation.stopping_criteria': {
        'StoppingCriteriaList': ('__call__',), 'MaxLengthCriteria': ('__call__',),
        'EosTokenCriteria': ('__call__',)},
    'transformers.tokenization_utils_base': {
        'PreTrainedTokenizerBase': ('__call__', '_call_one'),
        'BatchEncoding': ('convert_to_tensors',)},
    'transformers.tokenization_utils_fast': {
        'PreTrainedTokenizerFast': ('_batch_encode_plus', '_encode_plus', 'set_truncation_and_padding')},
    'transformers.models.qwen2.tokenization_qwen2_fast': {'Qwen2TokenizerFast': ('__init__',)},
    'transformers.processing_utils': {'ProcessorMixin': ('_merge_kwargs',)},
    'transformers.feature_extraction_utils': {'BatchFeature': ('convert_to_tensors',)},
}
_TRANSFORMERS_CLASS_METHODS['transformers.generation.logits_process']['LogitsProcessorList'] = ('__call__',)
_EXTRA_CODE_MODULES = (
    'torch.utils._contextlib', 'torch.autograd.grad_mode',
    'transformers.utils.generic', 'transformers.utils.deprecation',
    'transformers.modeling_rope_utils', 'transformers.integrations.sdpa_attention',
    'transformers.activations',
)


def _ensure_supported_native_versions():
    try:
        supported = version('qwen-tts') == '0.1.1' and version('transformers') == '4.57.3'
    except PackageNotFoundError as error:
        raise RecoveryIdentityUnavailable('missing_native_package_metadata') from error
    if not supported:
        raise RecoveryIdentityUnavailable('unsupported_native_package_contract')


def _collect_declared_methods(callbacks, module_name, cls, names=None):
    required = names is not None
    names = sorted(vars(cls)) if names is None else names
    for name in names:
        # Configuration/model constructors and direct helpers only; inherited
        # serving entries are explicitly enumerated in the runtime boundary.
        value = vars(cls).get(name)
        if isinstance(value, (classmethod, staticmethod)):
            value = value.__func__
        if type(value) is FunctionType:
            callbacks[module_name + '.' + cls.__name__ + '.' + name] = value
        elif isinstance(value, property):
            for suffix, function in (('get', value.fget), ('set', value.fset)):
                if function is not None:
                    callbacks[module_name + '.' + cls.__name__ + '.' + name + '.' + suffix] = function
        elif required or name in ('forward', 'generate', 'decode', 'chunked_decode', 'from_pretrained'):
            raise RecoveryIdentityUnavailable('missing_native_code_entry')


def _capture_context_bindings(callbacks, torch):
    roles, state, visited = [], [], set()
    contexts = []
    def visit(function, depth=0):
        if depth > 32:
            raise RecoveryIdentityUnavailable('native_wrapper_depth_limit')
        if type(function) is MethodType:
            context = function.__self__
            if type(context) in (torch.no_grad, torch.inference_mode):
                if not any(value is context for value in contexts):
                    contexts.append(context)
                    role = 'torch_context.' + type(context).__name__ + '.' + str(len(contexts) - 1)
                    roles.append((context, role))
                    mode = getattr(context, 'mode', None)
                    if mode is not None and type(mode) is not bool:
                        raise RecoveryIdentityUnavailable('unsupported_native_context_mode')
                    state.append({'role': role, 'mode': mode})
            function = function.__func__
        if type(function) is not FunctionType or function in visited:
            return
        visited.add(function)
        if len(visited) > 4096:
            raise RecoveryIdentityUnavailable('native_wrapper_count_limit')
        for cell in function.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError as error:
                raise RecoveryIdentityUnavailable('empty_native_wrapper') from error
            if type(value) in (FunctionType, MethodType):
                visit(value, depth + 1)
        wrapped = getattr(function, '__wrapped__', None)
        if wrapped is not None:
            visit(wrapped, depth + 1)
    for _, callback in sorted(callbacks.items()):
        visit(callback)
    for context in contexts:
        for name in ('clone', '__enter__', '__exit__'):
            callbacks['torch_context.' + type(context).__name__ + '.' + name] = getattr(type(context), name)
    return roles, sorted(state, key=lambda item: (item['role'], str(item['mode'])))


def _get_native_custom_code_sha256(*, extra_callables=None, extra_code_files=(), bound_roles=(), state=None):
    """Bind the finite supported Python path and caller's captured live state."""
    _ensure_supported_native_versions()
    import torch
    callbacks, modules, class_roles = {}, {}, []
    for module_name, policy in _QWEN_CODE_MANIFEST.items():
        module = modules[module_name] = importlib.import_module(module_name)
        classes = policy['classes']
        if classes == 'all_declared_configs':
            classes = {name: None for name, value in vars(module).items()
                       if isinstance(value, type) and value.__module__ == module_name}
        if len(classes) > 64:
            raise RecoveryIdentityUnavailable('native_class_limit')
        for name, methods in classes.items():
            cls = getattr(module, name)
            if not isinstance(cls, type) or cls.__module__ != module_name:
                raise RecoveryIdentityUnavailable('unknown_native_class')
            class_roles.append((cls, module_name + '.' + name))
            _collect_declared_methods(callbacks, module_name, cls, methods)
        for name in policy['functions']:
            callbacks[module_name + '.' + name] = getattr(module, name)
    for module_name, classes in _TRANSFORMERS_CLASS_METHODS.items():
        module = modules[module_name] = importlib.import_module(module_name)
        for name, methods in classes.items():
            cls = getattr(module, name)
            class_roles.append((cls, module_name + '.' + name))
            _collect_declared_methods(callbacks, module_name, cls, methods)
    for name in _EXTRA_CODE_MODULES:
        modules[name] = importlib.import_module(name)
    if extra_callables:
        callbacks.update(extra_callables)
    context_roles, context_state = _capture_context_bindings(callbacks, torch)
    class_roles.extend((cls, 'torch.autograd.grad_mode.' + cls.__name__)
                       for cls in (torch.no_grad, torch.inference_mode))
    processor = modules['qwen_tts.core.models.processing_qwen3_tts']
    processor_state = {
        'defaults': processor.Qwen3TTSProcessorKwargs._defaults,
        'attributes': processor.Qwen3TTSProcessor.attributes,
        'tokenizer_class': processor.Qwen3TTSProcessor.tokenizer_class,
    }
    tokenizer_base = modules['transformers.tokenization_utils_base']
    return get_live_callable_sha256(callbacks,
        code_files=[module.__file__ for module in modules.values()] + list(extra_code_files),
        bound_roles=list(bound_roles) + context_roles,
        class_roles=class_roles,
        enum_roles=[(getattr(tokenizer_base, name), 'transformers.' + name)
                    for name in ('PaddingStrategy', 'TruncationStrategy')],
        state={'contract': 'qwen-tts0.1.1-transformers4.57.3-custom12hz-v1',
               'processor': processor_state, 'contexts': context_state, 'serving': state})


def get_native_custom_code_sha256(*, extra_callables=None, extra_code_files=(), bound_roles=(), state=None):
    """Unknown optional package layouts refuse identity without blocking synthesis."""
    try:
        return _get_native_custom_code_sha256(extra_callables=extra_callables,
            extra_code_files=extra_code_files, bound_roles=bound_roles, state=state)
    except (ImportError, AttributeError, KeyError) as error:
        raise RecoveryIdentityUnavailable('unavailable_native_code_contract') from error


# The scope is installed only for an eligible owned CustomVoice split root.
# Disabled/ordinary warmup never performs a model inventory or weight hash.
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from synthesis_recovery_identity import (capture_recovery_model_snapshot,
    ensure_recovery_model_snapshot_current, get_recovery_digest)

_capture_model_load = ContextVar('capture_native_recovery_model_load', default=False)
_CUSTOM_MODEL_ID = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'


@dataclass(frozen=True)
class NativeRecoveryLoadProposal:
    snapshot: object
    code_sha256: str
    options: tuple


@dataclass(frozen=True)
class NativeRecoveryLoadedModel:
    asset_sha256: str
    loader_code_sha256: str
    options_sha256: str


@contextmanager
def ensure_native_recovery_model_capture():
    token = _capture_model_load.set(True)
    try:
        yield
    finally:
        _capture_model_load.reset(token)


def capture_native_recovery_load(model_cls, model_id, local_path, load_kwargs):
    """Prepare evidence before a known actual local load; no retroactive labels."""
    if not _capture_model_load.get():
        return None
    if model_id != _CUSTOM_MODEL_ID or not local_path:
        raise RecoveryIdentityUnavailable('unqualified_model_source')
    _ensure_supported_native_versions()
    try:
        expected = importlib.import_module('qwen_tts.inference.qwen3_tts_model').Qwen3TTSModel
        import torch
        dtype_names = ((torch.float16, 'float16'), (torch.bfloat16, 'bfloat16'), (torch.float32, 'float32'))
    except (ImportError, AttributeError) as error:
        raise RecoveryIdentityUnavailable('unavailable_native_loader_contract') from error
    if model_cls is not expected:
        raise RecoveryIdentityUnavailable('unknown_native_loader')
    if type(load_kwargs) is not dict or set(load_kwargs) - {'dtype', 'device_map'}:
        raise RecoveryIdentityUnavailable('unsupported_native_load_options')
    dtype_name = next((name for dtype, name in dtype_names if load_kwargs.get('dtype') is dtype), None)
    if dtype_name is None:
        raise RecoveryIdentityUnavailable('unsupported_native_load_options')
    device = load_kwargs.get('device_map', 'cpu')
    if (type(device) is not str or device not in ('cpu', 'mps')
            and not (device.startswith('cuda:') and device[5:].isascii() and device[5:].isdecimal())):
        raise RecoveryIdentityUnavailable('unsupported_native_load_device')
    from audio_generation_owner import ensure_audio_generation_active
    code_sha256 = get_native_custom_code_sha256()
    snapshot = capture_recovery_model_snapshot(local_path, check_active=ensure_audio_generation_active)
    options = (('device', device), ('dtype', dtype_name))
    return NativeRecoveryLoadProposal(snapshot, code_sha256, options)


def complete_native_recovery_load(model, proposal):
    """Record evidence only after the actual admitted local load succeeds."""
    if type(proposal) is not NativeRecoveryLoadProposal:
        raise RecoveryIdentityUnavailable('missing_native_load_proposal')
    try:
        expected = importlib.import_module('qwen_tts.inference.qwen3_tts_model').Qwen3TTSModel
    except (ImportError, AttributeError) as error:
        raise RecoveryIdentityUnavailable('unavailable_native_loader_contract') from error
    if type(model) is not expected:
        raise RecoveryIdentityUnavailable('unknown_loaded_native_model')
    ensure_recovery_model_snapshot_current(proposal.snapshot)
    if get_native_custom_code_sha256() != proposal.code_sha256:
        raise RecoveryIdentityUnavailable('native_loader_code_changed')
    model._aa2_recovery_loaded_model = NativeRecoveryLoadedModel(
        proposal.snapshot.sha256, proposal.code_sha256, get_recovery_digest(dict(proposal.options)))


def get_native_recovery_load_observation(model):
    """Read historical load evidence without certifying current model state."""
    capture = getattr(model, '__dict__', {}).get('_aa2_recovery_loaded_model')
    if type(capture) is not NativeRecoveryLoadedModel:
        return None
    return {'scope': 'captured_at_load_not_revalidated',
            'asset_sha256': capture.asset_sha256,
            'loader_code_sha256': capture.loader_code_sha256,
            'options_sha256': capture.options_sha256}


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
