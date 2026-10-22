"""Partial release cannot silently admit work; memory domains remain distinct."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import device_utils
import tts


class ResidencyReleaseFailureTests(unittest.TestCase):
    def test_cache_failure_after_drop_stays_pending_until_actual_cleanup(self):
        engine = tts.TTSEngine({'tts': {}})
        engine._local_custom_model = object()
        engine._local_model_devices['custom'] = 'cpu'
        with patch.object(engine, '_clear_gpu_cache', side_effect=RuntimeError('private failure path')):
            with self.assertRaises(RuntimeError):
                engine.ensure_idle_local_models_released()
        report = engine.get_model_residency()
        self.assertIsNone(engine._local_custom_model)
        self.assertTrue(report['cleanup_pending'])
        self.assertEqual('failed', report['release_events'][-1]['result'])
        self.assertEqual('clear_cache', report['release_events'][-1]['phase'])
        self.assertNotIn('private failure path', str(report))
        with patch.object(engine, '_clear_gpu_cache') as cleanup:
            self.assertTrue(engine.ensure_idle_local_models_released())
        cleanup.assert_called_once_with()
        self.assertFalse(engine.get_model_residency()['cleanup_pending'])
        self.assertEqual('released', engine.get_model_residency()['release_events'][-1]['result'])

    def test_cache_family_failure_is_retried_even_after_model_slot_is_empty(self):
        engine = tts.TTSEngine({'tts': {}})
        engine._local_clone_model = object()
        engine._local_model_devices['clone'] = 'cpu'
        with patch.object(engine, '_clear_clone_reference_caches', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                engine.ensure_idle_local_models_released()
        self.assertIsNone(engine._local_clone_model)
        with patch.object(engine, '_clear_clone_reference_caches') as clear, \
             patch.object(engine, '_clear_gpu_cache'):
            self.assertTrue(engine.ensure_idle_local_models_released())
        clear.assert_called_once_with()

    def test_release_history_is_bounded_and_returns_independent_copies(self):
        engine = tts.TTSEngine({'tts': {}})
        with patch.object(engine, '_clear_gpu_cache'):
            for _ in range(12):
                engine._local_custom_model = object()
                engine._local_model_devices['custom'] = 'cpu'
                self.assertTrue(engine.ensure_idle_local_models_released())
        report = engine.get_model_residency()
        self.assertEqual(8, len(report['release_events']))
        report['release_events'].clear()
        self.assertEqual(8, len(engine.get_model_residency()['release_events']))

    def test_cuda_rocm_and_apple_counters_keep_separate_meanings(self):
        cuda = SimpleNamespace(memory_allocated=lambda d: 3, memory_reserved=lambda d: 7,
                               mem_get_info=lambda d: (11, 19))
        torch = SimpleNamespace(cuda=cuda, version=SimpleNamespace(hip='fixture'),
            mps=SimpleNamespace(current_allocated_memory=lambda: 5,
                                driver_allocated_memory=lambda: 13,
                                recommended_max_memory=lambda: 17))
        with patch.dict(sys.modules, {'torch': torch}):
            rocm = device_utils.get_loaded_torch_memory_observation('cuda:0')
            mps = device_utils.get_loaded_torch_memory_observation('mps')
        self.assertEqual('rocm_vram', rocm['kind'])
        self.assertEqual(11, rocm['metrics']['driver_free_bytes'])
        self.assertEqual('apple_unified', mps['kind'])
        self.assertEqual(17, mps['metrics']['recommended_max_bytes'])
        self.assertNotIn('driver_total_bytes', mps['metrics'])
        self.assertNotIn('driver_free_bytes', mps['metrics'])

    def test_missing_or_invalid_memory_readings_are_unknown(self):
        with patch.dict(sys.modules, {'torch': None}):
            report = device_utils.get_loaded_torch_memory_observation('cuda:0')
        self.assertTrue(all(value is None for value in report['metrics'].values()))
        self.assertIsNone(device_utils.get_loaded_torch_memory_observation('cpu')['metrics']['model_allocated_bytes'])
