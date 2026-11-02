"""MLX worker LoRA (#1442 criterion 4), on CPU with numpy stand-ins for mlx.

The stand-in Linear computes x @ W.T like mlx.nn.Linear, so the wrapper's
arithmetic is checked against the PEFT merge formula W + (alpha/r) * B @ A.
Native exactness against PEFT itself is qualified separately on Apple Silicon.
"""
from pathlib import Path
import importlib
import json
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

APP_DIR = Path(__file__).resolve().parent.parent
PEFT = 'base_model.model.'


class FakeModule:
    def __init__(self):
        pass

    def parameters(self):
        return {}


class FakeLinear(FakeModule):
    def __init__(self, weight):
        super().__init__()
        self.weight = weight

    def __call__(self, x):
        return x @ self.weight.T


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class LoRAWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.loaded = {}
        core = types.SimpleNamespace(float32=np.float32, eval=lambda *values: None,
                                     load=lambda path: dict(self.loaded[path]),
                                     random=types.SimpleNamespace(seed=lambda value: None), clear_cache=lambda: None)
        nn = types.SimpleNamespace(Module=FakeModule, Linear=FakeLinear)
        mlx = types.ModuleType('mlx'); mlx.core = core; mlx.nn = nn
        patcher = mock.patch.dict(sys.modules, {'mlx': mlx, 'mlx.core': core, 'mlx.nn': nn})
        patcher.start(); self.addCleanup(patcher.stop)
        sys.modules.pop('mlx_tts_worker', None)
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        self.worker = importlib.import_module('mlx_tts_worker')
        self.addCleanup(sys.modules.pop, 'mlx_tts_worker', None)
        rng = np.random.default_rng(0)
        self.q = FakeLinear(rng.standard_normal((6, 4)).astype(np.float32))
        self.v = FakeLinear(rng.standard_normal((3, 4)).astype(np.float32))
        layer = ns(self_attn=ns(q_proj=self.q, v_proj=self.v))
        self.model = ns(talker=ns(model=ns(layers=[layer])))

    def adapter(self, name, tensors, **config):
        folder = self.tmp / name
        folder.mkdir()
        config = dict({'r': 2, 'lora_alpha': 8, 'target_modules': ['q_proj', 'v_proj']}, **config)
        (folder / 'adapter_config.json').write_text(json.dumps(config))
        self.loaded[str(folder / 'adapter_model.safetensors')] = tensors
        return str(folder)

    def tensors(self, seed=1, out=(6, 3)):
        rng = np.random.default_rng(seed)
        t = {}
        for proj, rows in zip(('q_proj', 'v_proj'), out):
            path = f'{PEFT}model.layers.0.self_attn.{proj}'
            t[f'{path}.lora_A.weight'] = rng.standard_normal((2, 4)).astype(np.float32)
            t[f'{path}.lora_B.weight'] = rng.standard_normal((rows, 2)).astype(np.float32)
        return t

    def test_wrapper_equals_the_peft_merge_formula(self):
        tensors = self.tensors()
        info = self.worker.ensure_adapter(self.model, self.adapter('a', tensors), 'gen-a')
        self.assertEqual((2, 4.0), (info['modules'], info['scale']))
        x = np.random.default_rng(5).standard_normal((3, 4)).astype(np.float32)
        a = tensors[f'{PEFT}model.layers.0.self_attn.q_proj.lora_A.weight']
        b = tensors[f'{PEFT}model.layers.0.self_attn.q_proj.lora_B.weight']
        merged = self.q.weight + 4.0 * (b @ a)
        wrapped = self.model.talker.model.layers[0].self_attn.q_proj
        self.assertIsNot(wrapped, self.q)
        np.testing.assert_allclose(wrapped(x), x @ merged.T, rtol=1e-5, atol=1e-5)
        np.testing.assert_array_equal(self.q(x), x @ self.q.weight.T)  # the base projection is untouched

    def test_output_keeps_the_input_dtype(self):
        self.q.weight = self.q.weight.astype(np.float16)  # a half-precision base, as in bf16 serving
        self.worker.ensure_adapter(self.model, self.adapter('a', self.tensors()), 'gen-a')
        x = np.ones((1, 4), np.float16)
        self.assertEqual(np.float16, self.model.talker.model.layers[0].self_attn.q_proj(x).dtype)

    def test_swap_and_remove_restore_the_original_projections(self):
        self.worker.ensure_adapter(self.model, self.adapter('a', self.tensors(1)), 'gen-a')
        first = self.model.talker.model.layers[0].self_attn.q_proj
        self.worker.ensure_adapter(self.model, str(self.tmp / 'not-read'), 'gen-a')  # same generation: no reload
        self.assertIs(first, self.model.talker.model.layers[0].self_attn.q_proj)
        self.worker.ensure_adapter(self.model, self.adapter('b', self.tensors(2)), 'gen-b')
        second = self.model.talker.model.layers[0].self_attn.q_proj
        self.assertIsNot(first, second)
        self.assertIs(second.base, self.q)  # wraps the original, never a wrapper of a wrapper
        self.worker.ensure_adapter_removed(self.model)
        self.assertIs(self.model.talker.model.layers[0].self_attn.q_proj, self.q)
        self.assertIs(self.model.talker.model.layers[0].self_attn.v_proj, self.v)

    def test_shape_mismatch_refuses_before_swapping_anything(self):
        bad = self.tensors(out=(6, 5))  # v_proj B has the wrong output size
        with self.assertRaisesRegex(ValueError, 'do not fit'):
            self.worker.ensure_adapter(self.model, self.adapter('bad', bad), 'gen-bad')
        self.assertIs(self.model.talker.model.layers[0].self_attn.q_proj, self.q)
        self.assertIsNone(self.worker._adapter['key'])

    def test_missing_target_module_refuses(self):
        tensors = self.tensors()
        tensors[f'{PEFT}model.layers.7.self_attn.q_proj.lora_A.weight'] = np.zeros((2, 4), np.float32)
        tensors[f'{PEFT}model.layers.7.self_attn.q_proj.lora_B.weight'] = np.zeros((6, 2), np.float32)
        with self.assertRaisesRegex(ValueError, 'does not have'):
            self.worker.ensure_adapter(self.model, self.adapter('far', tensors), 'gen-far')
        self.assertIs(self.model.talker.model.layers[0].self_attn.q_proj, self.q)

    def test_config_refusals_name_the_unsupported_feature(self):
        good = {'r': 32, 'lora_alpha': 128, 'target_modules': ['q_proj', 'k_proj', 'v_proj', 'o_proj']}
        self.assertEqual(4.0, self.worker.get_lora_scale(good))
        for change, message in (({'use_dora': True}, 'use_dora'), ({'use_rslora': True}, 'use_rslora'),
                                ({'fan_in_fan_out': True}, 'fan_in_fan_out'), ({'bias': 'all'}, 'bias'),
                                ({'modules_to_save': ['lm_head']}, 'modules_to_save'),
                                ({'rank_pattern': {'q_proj': 8}}, 'rank_pattern'),
                                ({'target_modules': ['q_proj', 'gate_proj']}, 'gate_proj'),
                                ({'r': 0}, 'positive'), ({'lora_alpha': None}, 'positive')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.worker.get_lora_scale(dict(good, **change))

    def test_key_mapping_accepts_peft_pairs_and_rejects_everything_else(self):
        keys = [f'{PEFT}model.layers.0.self_attn.q_proj.lora_A.weight',
                f'{PEFT}model.layers.0.self_attn.q_proj.lora_B.weight',
                f'{PEFT}code_predictor.model.layers.1.self_attn.o_proj.lora_A.weight',
                f'{PEFT}code_predictor.model.layers.1.self_attn.o_proj.lora_B.weight']
        self.assertEqual({'model.layers.0.self_attn.q_proj', 'code_predictor.model.layers.1.self_attn.o_proj'},
                         set(self.worker.get_lora_targets(keys)))
        for bad, message in ((keys[:1], 'no matching lora_B'), (keys[1:2], 'unpaired'),
                             (keys + ['base_model.model.model.norm.weight'], 'Unrecognised'),
                             ([k.replace('.weight', '.default.weight') for k in keys[:2]], 'Unrecognised'),
                             ([k.replace('q_proj', 'up_proj') for k in keys[:2]], 'up_proj')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                self.worker.get_lora_targets(bad)

    def test_instruct_hook_is_removed_after_success_and_after_failure(self):
        calls = []

        class Model:
            def _prepare_generation_inputs(self, text, **kwargs):
                calls.append(kwargs.get('instruct'))
        model = Model()
        with self.worker.ensure_instruct(model, 'Softly.'):
            model._prepare_generation_inputs('x')
            model._prepare_generation_inputs('x', instruct='explicit')
        model._prepare_generation_inputs('x')
        with self.assertRaises(RuntimeError), self.worker.ensure_instruct(model, 'Loud.'):
            raise RuntimeError('render failed')
        model._prepare_generation_inputs('x')
        with self.worker.ensure_instruct(model, None):
            model._prepare_generation_inputs('x')
        self.assertEqual(['Softly.', 'explicit', None, None, None], calls)
        self.assertNotIn('_prepare_generation_inputs', vars(model))


if __name__ == '__main__':
    unittest.main()
