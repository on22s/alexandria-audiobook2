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
    def test_clone_admission_refuses_invalid_references_and_policies_before_loading(self):
        from unittest.mock import Mock
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'compile_codec': False}})
        request = tts.CloneRequest('/private/reference.wav', 'Synthetic reference.', 42)
        loader = Mock(side_effect=AssertionError('must not load'))
        expected = {**tts._RAW_CLONE_METHODS, '_init_local_clone': loader}
        with patch.object(engine, '_init_local_clone', loader), patch.object(tts, '_RAW_CLONE_METHODS', expected):
            with patch('voice_manifest.get_adapter_asset_snapshot', side_effect=FileNotFoundError('missing')):
                with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'clone_reference_or_loader_unavailable'):
                    serving.capture_native_serving_identity(engine, request)
            with patch.object(engine, '_load_model_attempt', Mock()), patch('voice_manifest.get_adapter_asset_snapshot') as snapshot:
                with self.assertRaises(RecoveryIdentityUnavailable):
                    serving.capture_native_serving_identity(engine, request)
                snapshot.assert_not_called()
            for bad in (tts.CloneRequest(request.ref_audio_path, '', 42),
                        tts.CloneRequest(request.ref_audio_path, request.ref_text, -1),
                        tts.CloneRequest(request.ref_audio_path, request.ref_text, 2**64)):
                with self.subTest(request=bad), self.assertRaises(RecoveryIdentityUnavailable):
                    serving.capture_native_clone_serving_identity(engine, bad)
        loader.assert_not_called()

    def test_clone_dispatch_binds_reference_and_request_and_rejects_drift(self):
        import torch
        from unittest.mock import Mock
        from synthesis_recovery_native import NativeRecoveryLoadedModel
        from synthesis_recovery_identity import get_recovery_digest
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'compile_codec': False}})
        wrapper = SimpleNamespace(model=SimpleNamespace(dtype=torch.float32, device=torch.device('cpu')),
            _aa2_recovery_loaded_model=NativeRecoveryLoadedModel('a'*64, 'b'*64, 'c'*64))
        loader = Mock(return_value=wrapper)
        expected = {**tts._RAW_CLONE_METHODS, '_init_local_clone': loader}
        request = tts.CloneRequest('/private/reference.wav', 'Synthetic reference.', 42)
        def project(**kwargs):
            return get_recovery_digest(kwargs['state']) if kwargs else 'b'*64
        with patch.object(engine, '_init_local_clone', loader), patch.object(tts, '_RAW_CLONE_METHODS', expected), patch.object(serving, '_ensure_class', side_effect=lambda value, *args: value), patch.object(serving, 'get_native_custom_code_sha256', side_effect=project), patch.object(serving, '_get_runtime_build', return_value={'fixture': 'cpu'}), patch.object(serving, '_get_aa2_loaded_code', return_value={}), patch('voice_manifest.get_adapter_asset_snapshot', return_value=(request.ref_audio_path, b'first reference')) as snapshot, patch.object(serving, 'capture_native_custom_serving_identity', side_effect=AssertionError('wrong dispatch')):
            identity = serving.capture_native_serving_identity(engine, request)
            with serving.ensure_native_recovery_serving(engine, identity):
                serving.ensure_native_recovery_serving_current(engine, request)
                snapshot.return_value = (request.ref_audio_path, b'changed reference')
                with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                    serving.ensure_native_recovery_serving_current(engine, request)
                snapshot.return_value = (request.ref_audio_path, b'first reference')
                for changed in (tts.CloneRequest(request.ref_audio_path, 'Changed words.', 42),
                                tts.CloneRequest(request.ref_audio_path, request.ref_text, 43)):
                    with self.subTest(request=changed), self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                        serving.ensure_native_recovery_serving_current(engine, changed)
                engine._max_new_tokens += 1
                with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                    serving.ensure_native_recovery_serving_current(engine, request)
                engine._max_new_tokens -= 1
                del wrapper._aa2_recovery_loaded_model
                with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'clone_model_loaded_without_identity'):
                    serving.capture_native_serving_identity(engine, request)

    def test_lora_refuses_replaced_loaders_before_calling_them(self):
        from unittest.mock import Mock
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'compile_codec': False}})
        request = tts.LoRARequest('/private/adapter', 'a' * 64, 42, '')
        for name in ('_ensure_local_lora_generation', '_init_local_lora', '_ensure_lora_prompt'):
            with self.subTest(method=name):
                replacement = Mock(side_effect=RuntimeError('must not load'))
                with patch.object(engine, name, replacement):
                    with self.assertRaises(RecoveryIdentityUnavailable):
                        serving.capture_native_lora_serving_identity(engine, request)
                replacement.assert_not_called()

    def test_lora_malformed_model_is_optional_and_merge_certificate_is_required(self):
        from unittest.mock import Mock
        from synthesis_recovery_native import NativeRecoveryLoadedModel
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'compile_codec': False}})
        request = tts.LoRARequest('/private/adapter', 'a' * 64, 42, '')
        class FixtureWrapper:
            pass
        wrapper = FixtureWrapper()
        wrapper._aa2_recovery_loaded_model = NativeRecoveryLoadedModel('a'*64, 'b'*64, 'c'*64)
        malformed = FixtureWrapper()
        malformed._aa2_recovery_loaded_model = wrapper._aa2_recovery_loaded_model
        malformed._aa2_recovery_lora_merge_sha256 = 'd'*64
        for model in (object(), wrapper, malformed):
            loader = Mock(return_value=(model, None))
            expected = {**tts._RAW_LORA_METHODS, '_ensure_local_lora_generation': loader}
            with self.subTest(model=type(model).__name__), patch.object(serving, '_get_declared_class', return_value=FixtureWrapper), patch.object(engine, '_ensure_local_lora_generation', loader), patch.object(tts, '_RAW_LORA_METHODS', expected), patch.object(serving, 'get_native_custom_code_sha256', return_value='b'*64), patch.object(serving, 'get_native_lora_merge_sha256', return_value='d'*64):
                with self.assertRaises(RecoveryIdentityUnavailable):
                    serving.capture_native_lora_serving_identity(engine, request)
                loader.assert_called_once()

    def test_lora_merge_identity_binds_peft_release_and_loaded_merge_code(self):
        from peft.tuners.lora.layer import Linear
        first = serving.get_native_lora_merge_sha256()
        self.assertEqual(first, serving.get_native_lora_merge_sha256())
        with patch.object(serving, 'version', return_value='different-release'):
            self.assertNotEqual(first, serving.get_native_lora_merge_sha256())
        def changed_merge(self, *args, **kwargs):
            raise AssertionError('identity inspection must not execute merge')
        with patch.object(Linear, 'merge', changed_merge):
            self.assertNotEqual(first, serving.get_native_lora_merge_sha256())
        self.assertEqual(first, serving.get_native_lora_merge_sha256())
        import torch
        from unittest.mock import Mock
        from synthesis_recovery_native import NativeRecoveryLoadedModel
        from synthesis_recovery_identity import get_recovery_digest
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'compile_codec': False}})
        wrapper = SimpleNamespace(model=SimpleNamespace(dtype=torch.float32, device=torch.device('cpu')))
        wrapper._aa2_recovery_loaded_model = NativeRecoveryLoadedModel('a'*64, 'b'*64, 'c'*64)
        wrapper._aa2_recovery_lora_merge_sha256 = first
        loader = Mock(return_value=(wrapper, None))
        request = tts.LoRARequest('/private/adapter', 'a'*64, 42, '')
        expected = {**tts._RAW_LORA_METHODS, '_ensure_local_lora_generation': loader}
        def project(**kwargs):
            return get_recovery_digest(kwargs['state']) if kwargs else 'b'*64
        # The fake loader/class/runtime isolate the real LoRA projection and
        # dispatch/serving guard; no native provider or merge is executed.
        with patch.object(engine, '_ensure_local_lora_generation', loader), patch.object(tts, '_RAW_LORA_METHODS', expected), patch.object(serving, '_ensure_class', side_effect=lambda value, *args: value), patch.object(serving, 'get_native_custom_code_sha256', side_effect=project), patch.object(serving, '_get_runtime_build', return_value={'fixture': 'cpu'}), patch.object(serving, '_get_aa2_loaded_code', return_value={}), patch.object(serving, 'get_native_lora_merge_sha256', return_value=first) as merge, patch.object(serving, 'capture_native_custom_serving_identity', side_effect=AssertionError('wrong dispatch')):
            identity = serving.capture_native_lora_serving_identity(engine, request)
            with serving.ensure_native_recovery_serving(engine, identity):
                serving.ensure_native_recovery_serving_current(engine, request)
                merge.return_value = 'e'*64
                with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                    serving.ensure_native_recovery_serving_current(engine, request)
                # A newly loaded model with the changed PEFT implementation gets
                # a different serving key, rather than reusing the old cache.
                wrapper._aa2_recovery_lora_merge_sha256 = 'e'*64
                changed = serving.capture_native_lora_serving_identity(engine, request)
                self.assertNotEqual(identity.sha256, changed.sha256)
                merge.return_value = first
                wrapper._aa2_recovery_lora_merge_sha256 = first
                engine._max_new_tokens += 1
                with self.assertRaises(serving.SynthesisRecoveryDependencyChanged):
                    serving.ensure_native_recovery_serving_current(engine, request)

    def test_speech_policy_loaded_code_is_bound_and_missing_certificate_refuses_recovery(self):
        import speech_policy
        from synthesis_recovery_identity import get_recovery_digest
        first = serving._get_aa2_loaded_code()
        digest = speech_policy._LOADED_RECOVERY_CODE_SHA256
        self.assertEqual(64, len(digest))
        self.assertEqual(digest, first['speech_policy'])
        from pathlib import Path
        source_path = Path(speech_policy.__file__)
        source = source_path.read_text()
        changed = source.replace(
            "has_trailing_punctuation(text, '.!?。！？')",
            "has_trailing_punctuation(text, '.!?。！？…')", 1)
        self.assertNotEqual(source, changed)
        versions = []
        for code in (source, changed):
            namespace = {'__name__': 'speech_policy_identity_fixture'}
            exec(compile(code, str(source_path), 'exec'), namespace)
            versions.append(namespace)
        self.assertFalse(versions[0]['is_sentence_terminated']('Wait…'))
        self.assertTrue(versions[1]['is_sentence_terminated']('Wait…'))
        self.assertNotEqual(versions[0]['_LOADED_RECOVERY_CODE_SHA256'],
                            versions[1]['_LOADED_RECOVERY_CODE_SHA256'])
        with patch.object(speech_policy, '_LOADED_RECOVERY_CODE_SHA256',
                          versions[1]['_LOADED_RECOVERY_CODE_SHA256']):
            second = serving._get_aa2_loaded_code()
        self.assertNotEqual(get_recovery_digest(first), get_recovery_digest(second))
        with patch.object(speech_policy, '_LOADED_RECOVERY_CODE_SHA256', None):
            with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'missing_loaded_aa2_code'):
                serving._get_aa2_loaded_code()
        self.assertEqual(first, serving._get_aa2_loaded_code())

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

    def test_new_loader_release_and_allocation_policy_overrides_are_unqualified(self):
        import gpu_stats
        import device_utils
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        for name in ('_load_model_attempt', '_release_inactive_models_after_oom',
                     '_release_local_model_families', '_release_local_model_family', '_clear_gpu_cache'):
            with self.subTest(method=name), patch.object(engine, name, lambda *args: None):
                with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'overridden_engine_method'):
                    serving._capture_engine_methods(engine, {}, [])
        changed = dict(engine._LOCAL_MODEL_IDS, custom='changed/model')
        for target, name, value in [(engine, '_LOCAL_MODEL_IDS', changed),
                (device_utils, 'is_allocation_failure', lambda error: False),
                (gpu_stats, 'OOM_MARKERS', ('changed',))]:
            with self.subTest(field=name), patch.object(target, name, value):
                with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'overridden_model_load_policy'):
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
