"""Review regressions exercise device admission, provider handoffs and publication."""
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import tts
from project import ProjectManager


class TtsReviewRegressions(unittest.TestCase):
    def test_selected_gpu_limits_disabled_sub_batches_and_snapshot(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1',
                                      'sub_batch_enabled': False}})
        model = NS(device='cuda:1', model=NS(talker=NS(config=NS(
            num_hidden_layers=28, num_key_value_heads=8,
            hidden_size=2048, num_attention_heads=16))))
        def free(device=None):
            return ((2 if str(device) == 'cuda:1' else 12) * 2**30, 16 * 2**30)
        with patch('torch.cuda.is_available', return_value=True), \
             patch('torch.cuda.mem_get_info', side_effect=free) as probe, \
             patch('torch.cuda.memory_reserved', return_value=0) as reserved, \
             patch('torch.cuda.memory_allocated', return_value=0) as allocated:
            limit = engine._estimate_max_batch_size(model, max_text_chars=100)
            self.assertIs(type(limit), int)
            self.assertEqual(3, limit)
            batches = engine._build_sub_batches(['words'] * 7, max_items=limit)
            self.assertEqual([(0, 3), (3, 6), (6, 7)], batches)
            engine._vram_snapshot()
            for mock in (probe, reserved, allocated):
                self.assertTrue(all(str(call.args[0]) == 'cuda:1'
                                    for call in mock.call_args_list))
            model.device = 'cpu'
            self.assertEqual(9999, engine._estimate_max_batch_size(model))

    def test_clone_and_lora_single_and_batch_use_configured_language(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cpu',
                                          'language': 'Japanese'}})
            calls = []
            def generate(**kwargs):
                calls.append(kwargs)
                count = len(kwargs['text']) if isinstance(kwargs['text'], list) else 1
                return [np.sin(np.arange(2400) / 30) * .1 for _ in range(count)], 24000
            model = NS(device='cpu', generate_voice_clone=generate)
            prompt = [NS(ref_code=None, ref_text='こんにちは')]
            voices = {'A': {'type': 'clone', 'ref_audio': str(root / 'ref.wav'),
                            'ref_text': 'こんにちは', 'seed': -1}}
            sf.write(root / 'ref.wav', np.sin(np.arange(2400) / 30) * .1, 24000)
            chunks = [{'index': 0, 'speaker': 'A', 'text': 'こんにちは', 'instruct': ''}]
            with patch.object(engine, '_init_local_clone', return_value=model), \
                 patch.object(engine, '_get_clone_prompt', return_value=prompt), \
                 patch.object(engine, '_ensure_local_lora_generation', return_value=(model, prompt)), \
                 patch.object(engine, '_clear_gpu_cache'), \
                 patch('torch.cuda.is_available', return_value=False):
                self.assertTrue(engine._local_generate_clone('こんにちは', 'A', voices, str(root / 'single.wav')))
                self.assertEqual([0], engine._local_batch_clone(chunks, voices, folder)['completed'])
                adapter = root / 'adapter'
                adapter.mkdir()
                data = {'type': 'lora', 'adapter_path': str(adapter), 'seed': -1}
                self.assertTrue(engine.generate_lora_voice('こんにちは', '', data, str(root / 'lora.wav')))
                self.assertEqual([0], engine._local_batch_lora(chunks, {'A': data}, folder)['completed'])
            self.assertEqual(4, len(calls))
            self.assertEqual(['Japanese'] * 4, [call.get('language') for call in calls])
            self.assertGreater(sf.info(root / 'single.wav').frames, 0)

    def test_external_clone_passes_language_to_provider(self):
        import threading
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / 'ref.wav'
            sf.write(reference, np.sin(np.arange(2400) / 30) * .1, 24000)
            captured = []
            def predict(*args, **kwargs):
                captured.append(args)
                return (str(reference),)
            engine = tts.TTSEngine({'tts': {'mode': 'external', 'language': 'Japanese'}})
            client = NS(predict=predict)
            with patch.dict('sys.modules', {'gradio_client': NS(handle_file=lambda path: path)}), \
                 patch.object(engine, '_external_endpoint', return_value=(client, threading.Lock())):
                self.assertTrue(engine._external_generate_clone('こんにちは', 'A',
                    {'A': {'ref_audio': str(reference), 'ref_text': 'こんにちは'}},
                    str(root / 'out.wav')))
            self.assertEqual('Japanese', captured[0][3])
            self.assertGreater(sf.info(root / 'out.wav').frames, 0)

    def test_late_merge_cancellation_preserves_previous_export(self):
        from pydub import AudioSegment
        import project
        for phase in ('load', 'export'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                manager = ProjectManager(folder)
                output = Path(folder, 'cloned_audiobook.mp3')
                output.write_bytes(b'previous export')
                cancelled = [False]
                def load(**kwargs):
                    cancelled[0] = phase == 'load'
                    return [({'speaker': 'A', 'text': 'words'}, AudioSegment.silent(duration=300))], 0
                original_export = project._export_audio_segment
                def export(*args, **kwargs):
                    original_export(*args, **kwargs)
                    cancelled[0] = True
                with patch.object(manager, '_load_chunks_with_audio', side_effect=load), \
                     patch('project._export_audio_segment', side_effect=export):
                    self.assertEqual((False, 'Merge cancelled'),
                                     manager.merge_audio(cancel_check=lambda: cancelled[0]))
                self.assertEqual(b'previous export', output.read_bytes())
                self.assertEqual([], list(Path(folder).glob('*.pending.*')))
