"""Allocation cleanup retry is bounded and separate from cache repair."""
import contextlib
import gc
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref
import torch
import tts
from audio_generation_owner import AudioGenerationCancelled
from tests import test_tts_model_lifecycle as lifecycle


class ModelLoadAllocationRetryTests(unittest.TestCase):
    def test_cached_allocation_failure_retries_same_source_once_after_cleanup(self):
        events = []
        model = SimpleNamespace()
        def load(source, **kwargs):
            events.append(source)
            if len(events) == 1:
                raise RuntimeError('HIP out of memory')
            return model
        factory = SimpleNamespace(from_pretrained=Mock(side_effect=load))
        with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value='/cached'):
            result = tts.TTSEngine._load_model(factory, 'id', {}, release_for_oom=lambda: events.append('cleanup'))
        self.assertIs(model, result)
        self.assertEqual(['/cached', 'cleanup', '/cached'], events)

    def test_exhaustion_nonallocation_and_cancellation_do_not_add_cleanup_retries(self):
        for error, expected_calls, cleanup_calls in [(MemoryError('fixture'), 2, 1),
                (AudioGenerationCancelled('cancelled after out of memory'), 1, 0),
                (RuntimeError('CUDA error: invalid device ordinal'), 1, 0), (KeyboardInterrupt(), 1, 0)]:
            with self.subTest(error=type(error).__name__):
                factory = SimpleNamespace(from_pretrained=Mock(side_effect=error))
                cleanup = Mock()
                with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None):
                    with self.assertRaises(type(error)):
                        tts.TTSEngine._load_model(factory, 'id', {}, release_for_oom=cleanup)
                self.assertEqual(expected_calls, factory.from_pretrained.call_count)
                self.assertEqual(cleanup_calls, cleanup.call_count)

    def test_failed_loader_frame_is_released_before_cleanup(self):
        refs = []
        def load(source, **kwargs):
            if not refs:
                allocated = torch.ones(30)
                refs.append(weakref.ref(allocated))
                raise MemoryError('fixture allocation')
            return SimpleNamespace()
        def cleanup():
            gc.collect()
            self.assertIsNone(refs[0]())
        with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None):
            tts.TTSEngine._load_model(SimpleNamespace(from_pretrained=load), 'id', {}, release_for_oom=cleanup)

    def test_failed_cleanup_prevents_retry(self):
        factory = SimpleNamespace(from_pretrained=Mock(side_effect=MemoryError('fixture')))
        with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'synchronization failed'):
                tts.TTSEngine._load_model(factory, 'id', {},
                    release_for_oom=Mock(side_effect=RuntimeError('synchronization failed')))
        factory.from_pretrained.assert_called_once()

    def test_real_engine_retry_evicts_retained_inactive_family(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with lifecycle.ModelLifecycleTests().provider(engine, Path(tmp), capacity=32e9):
                engine._init_local_custom()
                previous = weakref.ref(engine._local_custom_model)
                original = tts.TTSEngine._load_model_attempt
                attempts = []
                def attempt(*args):
                    attempts.append(args[1])
                    if len(attempts) == 1:
                        raise MemoryError('fixture load allocation')
                    self.assertIsNone(previous())
                    return original(*args)
                with patch.object(tts.TTSEngine, '_load_model_attempt', side_effect=attempt), \
                     patch('torch.cuda.synchronize') as synchronize:
                    engine._init_local_clone()
                self.assertEqual(2, len(attempts))
                synchronize.assert_called_once_with('cuda:1')
                self.assertIsNone(engine._local_custom_model)
                self.assertIsNotNone(engine._local_clone_model)
