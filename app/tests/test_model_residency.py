"""Engine ownership boundaries with CPU fixtures, not native accounting."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import tts
from tests import test_tts_model_lifecycle as lifecycle


class ModelResidencyTests(unittest.TestCase):
    def test_loaded_device_idle_release_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with lifecycle.ModelLifecycleTests().provider(engine, Path(tmp)):
                engine._init_local_custom()
                ref = weakref.ref(engine._local_custom_model)
                self.assertEqual([{'family': 'custom', 'device': 'cuda:1', 'load_capture': None}],
                                 engine.get_model_residency()['residents'])
                with patch('torch.cuda.synchronize') as synchronize:
                    self.assertTrue(engine.ensure_idle_local_models_released())
                    synchronize.assert_called_once_with('cuda:1')
                self.assertIsNone(ref())
                self.assertTrue(engine._custom_warmup_needed)
                self.assertEqual([], engine.get_model_residency()['residents'])
                engine._init_local_custom()
                self.assertIsNotNone(engine._local_custom_model)

    def test_sync_failure_retains_models_and_cache_then_releases_locks(self):
        engine = tts.TTSEngine({'tts': {}})
        model = object()
        engine._local_lora_model = model
        engine._local_model_devices['lora'] = 'cuda:1'
        engine._lora_prompt_cache['fixture'] = 'retained'
        with patch('torch.cuda.synchronize', side_effect=RuntimeError('sync failed')):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                engine.ensure_idle_local_models_released()
        self.assertIs(model, engine._local_lora_model)
        self.assertEqual({'fixture': 'retained'}, engine._lora_prompt_cache)
        with patch('torch.cuda.synchronize'), patch.object(engine, '_clear_gpu_cache'):
            self.assertTrue(engine.ensure_idle_local_models_released())
        self.assertEqual({}, engine._lora_prompt_cache)

    def test_active_nested_exceptional_scopes_refuse_reentrant_release(self):
        engine = tts.TTSEngine({'tts': {}})
        @tts.ensure_local_tts_serialized
        def inner(owner):
            self.assertEqual(2, owner.get_model_residency()['active_leases'])
            self.assertFalse(owner.ensure_idle_local_models_released())
            raise KeyboardInterrupt('cancel fixture')
        @tts.ensure_local_tts_serialized
        def outer(owner):
            inner(owner)
        with self.assertRaises(KeyboardInterrupt):
            outer(engine)
        self.assertEqual(0, engine.get_model_residency()['active_leases'])
        self.assertTrue(engine.ensure_idle_local_models_released())

    def test_busy_read_and_release_do_not_wait_for_active_worker(self):
        engine = tts.TTSEngine({'tts': {}})
        entered, release = threading.Event(), threading.Event()
        @tts.ensure_local_tts_serialized
        def work(owner):
            entered.set()
            release.wait(5)
        thread = threading.Thread(target=work, args=(engine,))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual({'state': 'busy', 'active_leases': None, 'residents': None},
                             engine.get_model_residency())
            self.assertFalse(engine.ensure_idle_local_models_released())
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(0, engine.get_model_residency()['active_leases'])

    def test_unknown_device_refuses_and_mps_uses_its_own_sync_and_cache(self):
        engine = tts.TTSEngine({'tts': {}})
        engine._local_design_model = object()
        self.assertFalse(engine.ensure_idle_local_models_released())
        engine._local_model_devices['design'] = 'mps'
        mps = SimpleNamespace(synchronize=Mock(), empty_cache=Mock())
        with patch.dict(sys.modules, {'torch': SimpleNamespace(mps=mps)}), \
             patch.object(engine, '_clear_gpu_cache'):
            self.assertTrue(engine.ensure_idle_local_models_released())
        mps.synchronize.assert_called_once_with()
        mps.empty_cache.assert_called_once_with()
        self.assertIsNone(engine._local_design_model)
