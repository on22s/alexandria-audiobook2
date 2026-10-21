"""Actual loader orchestration distinguishes captured local loads from fallback."""
from contextlib import redirect_stdout, redirect_stderr
import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from audio_generation_owner import AudioGenerationCancelled
import synthesis_recovery_native as native
from synthesis_recovery_identity import RecoveryIdentityUnavailable
from tts import TTSEngine


class RecoveryLoadBridgeTests(unittest.TestCase):
    def model_class(self, result=None, error=None):
        cls = type('SyntheticModel', (), {})
        cls.from_pretrained = Mock(side_effect=error) if error else Mock(return_value=result or cls())
        return cls

    def test_disabled_local_loading_does_not_hash_or_import_native_models(self):
        cls = self.model_class()
        with patch.object(TTSEngine, '_resolve_local_model_path', return_value='/known/local'):
            with (patch.object(native, '_ensure_supported_native_versions', side_effect=AssertionError('native inspection')),
                  patch.object(native, 'capture_recovery_model_snapshot', side_effect=AssertionError('weight hashing'))):
                result = TTSEngine._load_model(cls, 'synthetic/model', {'dtype': 'unchanged'})
        self.assertIs(cls.from_pretrained.return_value, result)
        cls.from_pretrained.assert_called_once_with('/known/local', dtype='unchanged')
        self.assertFalse(hasattr(result, '_aa2_recovery_loaded_model'))

    def test_local_capture_uses_the_admitted_canonical_root_and_completes_after_success(self):
        cls = self.model_class()
        events = []
        proposal = SimpleNamespace(snapshot=SimpleNamespace(root='/admitted/canonical'))
        def capture(*args):
            events.append('capture')
            return proposal
        def load(*args, **kwargs):
            events.append('load')
            self.assertEqual(('/admitted/canonical',), args)
            return cls()
        def complete(model, received):
            events.append('complete')
            self.assertIs(proposal, received)
            self.assertIs(type(model), cls)
        cls.from_pretrained.side_effect = load
        with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/cache/alias'),
              patch.object(native, 'capture_native_recovery_load', side_effect=capture),
              patch.object(native, 'complete_native_recovery_load', side_effect=complete)):
            TTSEngine._load_model(cls, 'synthetic/model', {'dtype': 'unchanged'})
        self.assertEqual(['capture', 'load', 'complete'], events)

    def test_moving_id_fallback_never_inherits_local_capture(self):
        cls = self.model_class(error=[OSError('incomplete cached source'), SimpleNamespace()])
        proposal = SimpleNamespace(snapshot=SimpleNamespace(root='/admitted/canonical'))
        with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/cache/alias'),
              patch.object(native, 'capture_native_recovery_load', return_value=proposal),
              patch.object(native, 'complete_native_recovery_load') as complete,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = TTSEngine._load_model(cls, 'moving/model', {})
        self.assertEqual(['/admitted/canonical', 'moving/model'], [call.args[0] for call in cls.from_pretrained.call_args_list])
        complete.assert_not_called()
        self.assertFalse(hasattr(result, '_aa2_recovery_loaded_model'))

    def test_optional_identity_errors_preserve_ordinary_loading_but_owner_errors_abort(self):
        for error in (RecoveryIdentityUnavailable('unknown dependency'), OSError('unreadable asset')):
            cls = self.model_class()
            with self.subTest(error=type(error).__name__):
                with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/known/local'),
                      patch.object(native, 'capture_native_recovery_load', side_effect=error),
                      patch.object(native, 'complete_native_recovery_load') as complete):
                    TTSEngine._load_model(cls, 'synthetic/model', {})
                cls.from_pretrained.assert_called_once_with('/known/local')
                complete.assert_not_called()
        cls = self.model_class()
        with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/known/local'),
              patch.object(native, 'capture_native_recovery_load', side_effect=AudioGenerationCancelled())):
            with self.assertRaises(AudioGenerationCancelled):
                TTSEngine._load_model(cls, 'synthetic/model', {})
        cls.from_pretrained.assert_not_called()

    def test_preloaded_unrecorded_model_is_not_retroactively_labeled(self):
        engine = TTSEngine.__new__(TTSEngine)
        engine._local_custom_model = object()
        with (native.ensure_native_recovery_model_capture(),
              patch.object(native, 'capture_native_recovery_load', side_effect=AssertionError('retroactive hash'))):
            self.assertIs(engine._local_custom_model, engine._init_local_custom())
        self.assertFalse(hasattr(engine._local_custom_model, '_aa2_recovery_loaded_model'))

    def test_actual_capture_helper_binds_before_after_protocol_without_model_work(self):
        cls = self.model_class()
        dtype = object()
        torch = SimpleNamespace(float16=object(), bfloat16=dtype, float32=object())
        snapshot = SimpleNamespace(root='/admitted/canonical', sha256='a' * 64)
        events = []
        def capture(path, **kwargs):
            events.append('snapshot')
            self.assertEqual('/cache/alias', path)
            self.assertTrue(callable(kwargs['check_active']))
            return snapshot
        def verify(received):
            events.append('verify')
            self.assertIs(snapshot, received)
        with (patch.dict(sys.modules, {'torch': torch}),
              patch.object(native, '_ensure_supported_native_versions'),
              patch.object(native.importlib, 'import_module', return_value=SimpleNamespace(Qwen3TTSModel=cls)),
              patch.object(native, 'get_native_custom_code_sha256', return_value='b' * 64),
              patch.object(native, 'capture_recovery_model_snapshot', side_effect=capture),
              patch.object(native, 'ensure_recovery_model_snapshot_current', side_effect=verify)):
            self.assertIsNone(native.capture_native_recovery_load(cls, native._CUSTOM_MODEL_ID, '/cache/alias', {}))
            with native.ensure_native_recovery_model_capture():
                proposal = native.capture_native_recovery_load(cls, native._CUSTOM_MODEL_ID, '/cache/alias',
                                                               {'dtype': dtype, 'device_map': 'cuda:0'})
            model = cls()
            native.complete_native_recovery_load(model, proposal)
            self.assertEqual('a' * 64, model._aa2_recovery_loaded_model.asset_sha256)
            self.assertEqual('b' * 64, model._aa2_recovery_loaded_model.loader_code_sha256)
            self.assertEqual(['snapshot', 'verify'], events)
            with patch.object(native, 'get_native_custom_code_sha256', return_value='c' * 64):
                other = cls()
                with self.assertRaisesRegex(RecoveryIdentityUnavailable, 'native_loader_code_changed'):
                    native.complete_native_recovery_load(other, proposal)
                self.assertFalse(hasattr(other, '_aa2_recovery_loaded_model'))

    def test_missing_optional_metadata_or_layout_stays_temporary_and_owner_bugs_propagate(self):
        cls = self.model_class()
        with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/known/local'),
              native.ensure_native_recovery_model_capture(),
              patch.object(native, 'version', side_effect=native.PackageNotFoundError('synthetic metadata'))):
            TTSEngine._load_model(cls, native._CUSTOM_MODEL_ID, {})
        cls.from_pretrained.assert_called_once_with('/known/local')
        cls.from_pretrained.reset_mock()
        with (patch.object(TTSEngine, '_resolve_local_model_path', return_value='/known/local'),
              native.ensure_native_recovery_model_capture(),
              patch.object(native, '_ensure_supported_native_versions'),
              patch.object(native.importlib, 'import_module', return_value=SimpleNamespace())):
            TTSEngine._load_model(cls, native._CUSTOM_MODEL_ID, {})
        cls.from_pretrained.assert_called_once_with('/known/local')
        dtype = object()
        torch = SimpleNamespace(float16=object(), bfloat16=dtype, float32=object())
        with (native.ensure_native_recovery_model_capture(), patch.dict(sys.modules, {'torch': torch}),
              patch.object(native, '_ensure_supported_native_versions'),
              patch.object(native.importlib, 'import_module', return_value=SimpleNamespace(Qwen3TTSModel=cls)),
              patch.object(native, 'get_native_custom_code_sha256', return_value='b' * 64),
              patch.object(native, 'capture_recovery_model_snapshot', side_effect=AttributeError('owner callback failed'))):
            with self.assertRaisesRegex(AttributeError, 'owner callback failed'):
                native.capture_native_recovery_load(cls, native._CUSTOM_MODEL_ID, '/known/local', {'dtype': dtype})
