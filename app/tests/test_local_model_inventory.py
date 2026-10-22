"""Known loader models share a bounded, observation-only cache inventory."""
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import device_utils
import tts


class LocalModelInventoryTests(unittest.TestCase):
    def test_inventory_uses_loader_ids_and_collapses_shared_base(self):
        observed = {'state': 'required_assets_observed', 'content_sha256': None}
        with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value='/synthetic/cache') as resolve, \
             patch('synthesis_recovery_identity.get_model_asset_observation', return_value=observed) as inspect:
            rows = tts.TTSEngine.get_local_model_inventory()
        self.assertEqual(3, len(rows))
        self.assertEqual(set(tts.TTSEngine._LOCAL_MODEL_IDS.values()), {r['model_id'] for r in rows})
        self.assertEqual(3, resolve.call_count)
        self.assertEqual(3, inspect.call_count)
        base = next(r for r in rows if 'lora' in r['families'])
        self.assertEqual(['clone', 'lora'], base['families'])
        self.assertIsNone(base['content_sha256'])
        self.assertEqual(observed, {'state': 'required_assets_observed', 'content_sha256': None})

    def test_absent_and_failed_cache_never_load_or_download(self):
        for result, expected in [(None, 'not_cached'), (ImportError('optional hub absent'), 'unavailable'),
                                  (OSError('private path'), 'unavailable')]:
            with self.subTest(expected=expected), \
                 patch.object(tts.TTSEngine, '_resolve_local_model_path', side_effect=result if isinstance(result, Exception) else None, return_value=result), \
                 patch.object(tts.TTSEngine, '_load_model', side_effect=AssertionError('no model load')):
                rows = tts.TTSEngine.get_local_model_inventory()
            self.assertEqual({expected}, {row['state'] for row in rows})
            self.assertNotIn('private path', str(rows))

    def test_device_observation_does_not_import_torch_and_keeps_mps_separate(self):
        with patch.dict(sys.modules, {'torch': None}):
            self.assertEqual({'state': 'runtime_not_loaded', 'gpu_backend': None},
                             device_utils.get_loaded_torch_device_observation())
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=Mock(return_value=False)),
                                backends=SimpleNamespace(mps=SimpleNamespace(is_available=Mock(return_value=True))))
        with patch.dict(sys.modules, {'torch': torch}):
            self.assertEqual({'state': 'observed', 'gpu_backend': 'mps'},
                             device_utils.get_loaded_torch_device_observation())
        torch.cuda.is_available.side_effect = RuntimeError('driver failed')
        with patch.dict(sys.modules, {'torch': torch}):
            self.assertEqual({'state': 'unavailable', 'gpu_backend': None},
                             device_utils.get_loaded_torch_device_observation())
