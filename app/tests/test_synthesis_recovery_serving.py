"""Serving guards refuse dependency drift; transient tokenizer work stays semantic."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio_generation_owner import AudioGenerationCancelled
from synthesis_recovery_identity import RecoveryIdentityUnavailable
import synthesis_recovery_serving as serving
import tts


class RecoveryServingTests(unittest.TestCase):
    def test_processor_identity_binds_effective_call_policy(self):
        marker = object()
        processor = SimpleNamespace(_merge_kwargs=lambda kind, **kwargs: {
            'text_kwargs': {key: value for key, value in kwargs['tokenizer_init_kwargs'].items()
                            if key in ('max_length', 'truncation')},
            'call': [kind is marker, kwargs['return_tensors'], kwargs['padding']]})
        tokenizer = SimpleNamespace(init_kwargs={'max_length': 10, 'truncation': True,
                                                 'name_or_path': '/first'})
        module = SimpleNamespace(Qwen3TTSProcessorKwargs=marker)
        with patch.object(serving.importlib, 'import_module', return_value=module):
            first = serving._get_processor_call_kwargs(processor, tokenizer)
            tokenizer.init_kwargs['name_or_path'] = '/second'
            self.assertEqual(first, serving._get_processor_call_kwargs(processor, tokenizer))
            tokenizer.init_kwargs['max_length'] = 20
            self.assertNotEqual(first, serving._get_processor_call_kwargs(processor, tokenizer))
        self.assertEqual([True, 'pt', True], first['call'])

    def test_runtime_normalizes_known_torch_version_subclass(self):
        class TorchVersion(str):
            pass
        flags = SimpleNamespace(enabled=True, benchmark=False, deterministic=False, allow_tf32=False)
        cuda = SimpleNamespace(matmul=SimpleNamespace(allow_tf32=False,
            allow_fp16_reduced_precision_reduction=False, allow_bf16_reduced_precision_reduction=False),
            flash_sdp_enabled=lambda: False, mem_efficient_sdp_enabled=lambda: False, math_sdp_enabled=lambda: True)
        torch = SimpleNamespace(__version__=TorchVersion('test-build'),
            version=SimpleNamespace(git_version='abc', cuda=None, hip=None),
            __config__=SimpleNamespace(show=lambda: 'build'), backends=SimpleNamespace(cudnn=flags, cuda=cuda),
            get_num_threads=lambda: 1, get_num_interop_threads=lambda: 1,
            are_deterministic_algorithms_enabled=lambda: False,
            is_deterministic_algorithms_warn_only_enabled=lambda: False,
            get_float32_matmul_precision=lambda: 'highest', is_grad_enabled=lambda: False,
            is_inference_mode_enabled=lambda: False)
        with patch.object(serving, 'version', return_value='test'):
            state = serving._get_runtime_build(torch, SimpleNamespace(type='cpu'))
        self.assertIs(type(state['torch']), str)
        self.assertEqual('test-build', state['torch'])

    def test_tokenizer_working_padding_changes_do_not_relabel_vocabulary(self):
        class Tokenizer:
            def __init__(self):
                self.data = {'model': {'vocab': {'alpha': 1}}, 'normalizer': {'type': 'NFC'},
                             'padding': None, 'truncation': None}
                self.backend_tokenizer = SimpleNamespace(to_str=lambda: json.dumps(self.data))
            def __len__(self):
                return 1
        tokenizer = Tokenizer()
        first = serving.get_native_tokenizer_semantic_sha256(tokenizer)
        tokenizer.data.update(padding={'direction': 'left'}, truncation={'strategy': 'longest_first'})
        self.assertEqual(first, serving.get_native_tokenizer_semantic_sha256(tokenizer))
        tokenizer.data['model']['vocab'] = {'beta': 1}
        self.assertNotEqual(first, serving.get_native_tokenizer_semantic_sha256(tokenizer))
        with patch.object(serving, 'MAX_TOKENIZER_BYTES', 1):
            with self.assertRaises(RecoveryIdentityUnavailable):
                serving.get_native_tokenizer_semantic_sha256(tokenizer)

    def test_equal_proven_identity_after_reload_continues_but_changed_or_unproven_aborts(self):
        engine, request = object(), object()
        first = serving.NativeRecoveryServingIdentity('a' * 64, 'b' * 64, 'c' * 64)
        equal = serving.NativeRecoveryServingIdentity('a' * 64, 'b' * 64, 'c' * 64)
        changed = serving.NativeRecoveryServingIdentity('d' * 64, 'e' * 64, 'f' * 64)
        serving.ensure_native_recovery_serving_current(engine, request)  # Ordinary rendering does no inspection.
        with serving.ensure_native_recovery_serving(engine, first):
            with patch.object(serving, 'capture_native_custom_serving_identity', return_value=equal):
                serving.ensure_native_recovery_serving_current(engine, request)
            for result in (changed, RecoveryIdentityUnavailable('new unrecorded object')):
                with self.subTest(result=type(result).__name__):
                    kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                    with patch.object(serving, 'capture_native_custom_serving_identity', **kwargs):
                        with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                            serving.ensure_native_recovery_serving_current(engine, request)
        with patch.object(serving, 'capture_native_custom_serving_identity', side_effect=AssertionError('scope leaked')):
            serving.ensure_native_recovery_serving_current(engine, request)

    def test_wrong_engine_and_cancelled_owner_never_become_optional_cache_misses(self):
        engine, request = object(), object()
        identity = serving.NativeRecoveryServingIdentity('a' * 64, 'b' * 64, 'c' * 64)
        with serving.ensure_native_recovery_serving(engine, identity):
            with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                serving.ensure_native_recovery_serving_current(object(), request)
            with patch.object(serving, 'capture_native_custom_serving_identity', side_effect=AudioGenerationCancelled()):
                with self.assertRaises(AudioGenerationCancelled):
                    serving.ensure_native_recovery_serving_current(engine, request)

    def test_unknown_loaded_object_is_not_labeled_from_current_model_files(self):
        request = tts.CustomVoiceRequest('Ryan', 42, 'neutral')
        engine = SimpleNamespace(_language='English', _compile_codec_enabled=False,
            get_voice_backend=lambda kind: 'local', _init_local_custom=lambda: object())
        with patch.object(serving, 'get_native_custom_code_sha256', side_effect=AssertionError('unrecorded model inspection')):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'model_loaded_without_identity'):
                serving.capture_native_custom_serving_identity(engine, request)
        def cancelled_load():
            raise AudioGenerationCancelled()
        engine._init_local_custom = cancelled_load
        with self.assertRaises(AudioGenerationCancelled):
            serving.capture_native_custom_serving_identity(engine, request)

    def test_random_external_compiled_or_unsupported_requests_skip_model_initialization(self):
        def unexpected():
            self.fail('ineligible request initialized a model')
        engine = SimpleNamespace(_language='English', _compile_codec_enabled=False,
            get_voice_backend=lambda kind: 'local', _init_local_custom=unexpected)
        for request in (tts.CustomVoiceRequest('Ryan', -1, 'neutral'), tts.CustomVoiceRequest('Ryan', 2**64, 'neutral')):
            with self.subTest(seed=request.seed), self.assertRaises(RecoveryIdentityUnavailable):
                serving.capture_native_custom_serving_identity(engine, request)
        request = tts.CustomVoiceRequest('Ryan', 42, 'neutral')
        engine._compile_codec_enabled = True
        with self.assertRaises(RecoveryIdentityUnavailable):
            serving.capture_native_custom_serving_identity(engine, request)
        engine._compile_codec_enabled = False
        engine.get_voice_backend = lambda kind: 'external'
        with self.assertRaises(RecoveryIdentityUnavailable):
            serving.capture_native_custom_serving_identity(engine, request)

    def test_actual_engine_method_and_module_function_overrides_are_unqualified(self):
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        callbacks, files = {}, []
        serving._capture_engine_methods(engine, callbacks, files)
        self.assertTrue(callbacks)
        engine.generate_voice = lambda *args: True
        with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'overridden_engine_method'):
            serving._capture_engine_methods(engine, {}, [])
        del engine.generate_voice
        with patch.object(tts, 'anchored_instruct', lambda *args: 'changed'):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'overridden_engine_function'):
                serving._capture_engine_methods(engine, {}, [])

    def test_unsupported_generation_modes_are_refused_and_config_paths_do_not_define_semantics(self):
        data = {'_name_or_path': '/private/loader', '_original_object_hash': 5678,
                'num_beams': 1, 'top_p': .9}
        config = SimpleNamespace(to_dict=lambda: data)
        state = serving._get_generation_state(SimpleNamespace(generation_config=config))
        self.assertEqual({'num_beams': 1, 'top_p': .9}, state)
        for name, value in (('num_beams', 2), ('sequence_bias', {'unsafe': .2}), ('stop_strings', ['early'])):
            data[name] = value
            with self.subTest(setting=name), self.assertRaises(RecoveryIdentityUnavailable):
                serving._get_generation_state(SimpleNamespace(generation_config=config))
            data.pop(name)

    def test_guard_scope_is_not_transferred_to_unrelated_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        engine, request = object(), object()
        identity = serving.NativeRecoveryServingIdentity('a' * 64, 'b' * 64, 'c' * 64)
        with serving.ensure_native_recovery_serving(engine, identity):
            with patch.object(serving, 'capture_native_custom_serving_identity', side_effect=AssertionError('guard leaked')):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(serving.ensure_native_recovery_serving_current, engine, request).result(timeout=5)
