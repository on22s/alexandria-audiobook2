"""Two-card fixtures cannot lend one card's memory to another card's identity."""
import subprocess
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class LocalGpuSelectionTests(unittest.TestCase):
    def check_nvidia(self, stdout, memory, identity, returncode=0):
        result = subprocess.CompletedProcess([], returncode, stdout, '')
        with patch.object(settings, 'run_rocm_smi_json', return_value=None), \
             patch.object(settings.subprocess, 'run', return_value=result) as run:
            self.assertEqual(memory, settings.get_local_vram_bytes())
            self.assertEqual(identity, settings.get_gpu_name_and_backend())
            for call in run.call_args_list:
                self.assertIn('--query-gpu=name,memory.total,memory.used', call.args[0])

    def test_one_nvidia_card_has_matching_name_and_memory(self):
        self.check_nvidia('Card zero, 16384, 2048\n',
                         (16384 * 1024**2, 2048 * 1024**2), ('Card zero', 'cuda'))

    def test_two_nvidia_cards_cannot_select_different_devices(self):
        self.check_nvidia('Card zero, 16384, 2048\nCard one, 49152, 4096\n',
                         None, (None, None))

    def test_unknown_or_failed_memory_retains_identity_without_dynamic_sizing(self):
        for memory in ('N/A, N/A', '16384, -1', '100, 101'):
            with self.subTest(memory=memory):
                self.check_nvidia('Card zero, ' + memory, None, ('Card zero', 'cuda'))
        self.check_nvidia('Card zero, 16384, 2048', None, (None, None), 1)

    def test_rocm_one_card_uses_combined_product_and_memory_probe(self):
        card = {'Card Series': 'AMD fixture', 'VRAM Total Memory (B)': '10000',
                'VRAM Total Used Memory (B)': '1000'}
        with patch.object(settings, 'run_rocm_smi_json', return_value={'card0': card}) as probe, \
             patch.object(settings.subprocess, 'run') as run:
            self.assertEqual((10000, 1000), settings.get_local_vram_bytes())
            self.assertEqual(('AMD fixture', 'rocm'), settings.get_gpu_name_and_backend())
            self.assertEqual(['--showmeminfo', 'vram', '--showproductname'], probe.call_args.args[0])
            run.assert_not_called()

    def test_rocm_multi_card_or_ambiguous_name_does_not_fall_through_to_another_backend(self):
        card = {'Card Series': 'AMD fixture', 'VRAM Total Memory (B)': '10000',
                'VRAM Total Used Memory (B)': '1000'}
        for data in ({'card0': card, 'card1': card}, {'card0': {}}, {'card0': 'bad'},
                     {'card0': dict(card, **{'Card Series': 'N/A'})}):
            with self.subTest(data=data), \
                 patch.object(settings, 'run_rocm_smi_json', return_value=data), \
                 patch.object(settings.subprocess, 'run') as run:
                self.assertIsNone(settings.get_local_vram_bytes())
                self.assertEqual((None, None), settings.get_gpu_name_and_backend())
                run.assert_not_called()

    def test_multi_gpu_profile_keeps_existing_reserve_and_conservative_settings(self):
        result = subprocess.CompletedProcess([], 0,
            'Small card, 8192, 4096\nLarge card, 49152, 1000\n', '')
        with patch.object(settings, 'run_rocm_smi_json', return_value=None), \
             patch.object(settings.subprocess, 'run', return_value=result):
            chosen = settings.get_safe_local_settings('qwen/qwen3-14b', False)
        self.assertEqual(settings.IDEAL_SETTINGS, {k: chosen[k] for k in settings.IDEAL_SETTINGS})
        self.assertEqual('GPU memory could not be measured', chosen['reason'])
        self.assertEqual(2 * 1024**3, settings._LOCAL_VRAM_RESERVE_BYTES)
