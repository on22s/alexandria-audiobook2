"""Apple MPS renders one sequence per request and loads eager attention (#1650).

On an M4 every left-padded two-line CustomVoice batch failed with "probability
tensor contains either inf, nan or element < 0" while the same lines rendered
one at a time. These tests pin the guard on CPU with model doubles; nothing here
runs MPS inference.
"""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
# Imported up front: patch.dict(sys.modules) below would otherwise drop a
# torch first imported inside it, and re-importing its C extension crashes.
import torch

import device_utils
import tts

NAN_ERROR = 'probability tensor contains either `inf`, `nan` or element < 0'


def make_engine():
    with contextlib.redirect_stdout(io.StringIO()):
        return tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cpu', 'language': 'English'}})


class MpsBatchSizeTests(unittest.TestCase):
    def test_mps_models_get_one_sequence_whatever_else_is_available(self):
        engine = make_engine()
        for device in ('mps', 'mps:0', torch.device('mps')):
            with self.subTest(device=str(device)), \
                    patch('torch.cuda.is_available', return_value=True):
                self.assertEqual(1, engine._estimate_max_batch_size(SimpleNamespace(device=device)))
        # A model that does not report its device falls back to the configured one.
        with patch.object(engine, '_resolve_device', return_value='mps'):
            self.assertEqual(1, engine._estimate_max_batch_size(SimpleNamespace()))

    def test_cpu_sizing_is_unchanged(self):
        engine = make_engine()
        with patch('torch.cuda.is_available', return_value=False):
            self.assertEqual(9999, engine._estimate_max_batch_size(SimpleNamespace(device='cpu')))


class MpsBatchShapeTests(unittest.TestCase):
    def custom_engine(self, generate):
        engine = make_engine()
        engine._init_local_custom = lambda: SimpleNamespace(
            device='mps', generate_custom_voice=Mock(side_effect=generate))
        engine.ensure_custom_warmup = Mock()
        engine._clear_gpu_cache = Mock()
        return engine

    def test_custom_batch_on_mps_sends_one_line_per_request_and_keeps_failures_visible(self):
        calls = []
        def generate(**kwargs):
            calls.append(list(kwargs['text']))
            if kwargs['text'] == ['middle length line']:
                raise RuntimeError(NAN_ERROR)
            return [np.sin(np.arange(2400) / 30) * .1 for _ in kwargs['text']], 24000
        engine = self.custom_engine(generate)
        config = {'A': {'type': 'custom', 'voice': 'Ryan'},
                  'B': {'type': 'custom', 'voice': 'Serena'}}
        chunks = [{'index': 1, 'speaker': 'A', 'text': 'short', 'instruct': ''},
                  {'index': 2, 'speaker': 'B', 'text': 'middle length line', 'instruct': ''},
                  {'index': 3, 'speaker': 'A', 'text': 'the longest line of the three', 'instruct': ''}]
        with tempfile.TemporaryDirectory() as tmp, \
                patch('torch.cuda.is_available', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            result = engine._local_batch_custom(chunks, config, tmp)
            for index in (1, 3):
                audio, rate = sf.read(Path(tmp, f'temp_batch_{index}.wav'))
                self.assertEqual(24000, rate)
                self.assertTrue(np.isfinite(audio).all())
            self.assertFalse(Path(tmp, 'temp_batch_2.wav').exists())
        self.assertEqual([1], sorted({len(texts) for texts in calls}))
        self.assertEqual(3, len(calls))
        self.assertEqual([1, 3], sorted(result['completed']))
        self.assertEqual([2], [index for index, _ in result['failed']])
        # The real error reaches the user rather than a generic message.
        self.assertIn('probability tensor', result['failed'][0][1])

    def test_clone_and_lora_batches_on_mps_also_send_one_line_per_request(self):
        engine = make_engine()
        calls = []
        def generate(**kwargs):
            calls.append(kwargs['text'])
            count = len(kwargs['text']) if isinstance(kwargs['text'], list) else 1
            return [np.sin(np.arange(2400) / 30) * .1 for _ in range(count)], 24000
        model = SimpleNamespace(device='mps', generate_voice_clone=generate)
        prompt = [SimpleNamespace(ref_code=None, ref_text='hello there')]
        with tempfile.TemporaryDirectory() as folder, \
                contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            sf.write(root / 'ref.wav', np.sin(np.arange(2400) / 30) * .1, 24000)
            adapter = root / 'adapter'
            adapter.mkdir()
            clone = {'A': {'type': 'clone', 'ref_audio': str(root / 'ref.wav'),
                           'ref_text': 'hello there', 'seed': -1}}
            lora = {'A': {'type': 'lora', 'adapter_path': str(adapter), 'seed': -1}}
            chunks = [{'index': i, 'speaker': 'A', 'text': text, 'instruct': ''}
                      for i, text in enumerate(('one', 'a second line', 'and a rather longer third'))]
            with patch.object(engine, '_init_local_clone', return_value=model), \
                    patch.object(engine, '_get_clone_prompt', return_value=prompt), \
                    patch.object(engine, '_ensure_local_lora_generation', return_value=(model, prompt)), \
                    patch.object(engine, '_clear_gpu_cache'), \
                    patch('torch.cuda.is_available', return_value=False):
                self.assertEqual([0, 1, 2], sorted(engine._local_batch_clone(chunks, clone, folder)['completed']))
                self.assertEqual([0, 1, 2], sorted(engine._local_batch_lora(chunks, lora, folder)['completed']))
        self.assertEqual(6, len(calls))
        self.assertTrue(all(isinstance(text, list) and len(text) == 1 for text in calls), calls)


class MpsAttentionLoadTests(unittest.TestCase):
    def test_eager_attention_only_on_mps(self):
        self.assertEqual({'attn_implementation': 'eager'}, device_utils.get_attention_load_kwargs('mps'))
        self.assertEqual({'attn_implementation': 'eager'}, device_utils.get_attention_load_kwargs('mps:0'))
        for device in ('cpu', 'cuda', 'cuda:0', 'cuda:1'):
            with self.subTest(device=device):
                self.assertEqual({}, device_utils.get_attention_load_kwargs(device))

    def test_every_local_model_family_loads_with_the_device_attention_kwargs(self):
        qwen = ModuleType('qwen_tts')
        qwen.Qwen3TTSModel = type('Qwen3TTSModel', (), {})
        for device, expected in (('mps', {'attn_implementation': 'eager'}), ('cpu', {})):
            for family in ('custom', 'clone', 'design'):
                with self.subTest(device=device, family=family):
                    engine = make_engine()
                    captured = []
                    def load(model_cls, model_id, load_kwargs, **_):
                        captured.append(load_kwargs)
                        return SimpleNamespace(device=device)
                    with patch.dict(sys.modules, {'qwen_tts': qwen}), \
                            patch.object(engine, '_resolve_device', return_value=device), \
                            patch.object(engine, '_release_inactive_local_models'), \
                            patch.object(engine, '_load_model', side_effect=load), \
                            patch.object(engine, '_vram_snapshot', return_value={}), \
                            contextlib.redirect_stdout(io.StringIO()):
                        getattr(engine, f'_init_local_{family}')()
                    self.assertEqual(1, len(captured))
                    extra = {key: value for key, value in captured[0].items()
                             if key not in ('dtype', 'device_map')}
                    self.assertEqual(expected, extra)

    def test_lora_base_model_loads_with_the_device_attention_kwargs(self):
        # Stop at the load itself: the adapter merge after it is not under test.
        class Loaded(Exception):
            pass
        qwen = ModuleType('qwen_tts')
        qwen.Qwen3TTSModel = type('Qwen3TTSModel', (), {})
        peft = ModuleType('peft')
        peft.PeftModel = type('PeftModel', (), {})
        for device, expected in (('mps', {'attn_implementation': 'eager'}), ('cpu', {})):
            with self.subTest(device=device):
                engine = make_engine()
                captured = []
                def load(model_cls, model_id, load_kwargs, **_):
                    captured.append(load_kwargs)
                    raise Loaded()
                with patch.dict(sys.modules, {'qwen_tts': qwen, 'peft': peft}), \
                        patch.object(engine, '_resolve_device', return_value=device), \
                        patch.object(engine, '_release_inactive_local_models'), \
                        patch.object(engine, '_load_model', side_effect=load), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaises(Loaded):
                    engine._init_local_lora('/adapter', 'a' * 64)
                extra = {key: value for key, value in captured[0].items()
                         if key not in ('dtype', 'device_map')}
                self.assertEqual(expected, extra)


if __name__ == '__main__':
    unittest.main()
