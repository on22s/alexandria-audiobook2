"""Verify seeded LoRA initialization using actual CPU PEFT/safetensors artifacts."""
import contextlib
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import torch
import peft
import peft.tuners.lora.model as peft_lora_model
from safetensors.torch import load_file
import train_lora


class InitializationRecorded(Exception):
    pass


class SeededLoRAInitializationTests(unittest.TestCase):
    def get_initial_adapter(self, ambient_seed, requested_seed):
        before_torch = torch.random.get_rng_state()
        before_python = random.getstate()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'metadata.jsonl').write_text(json.dumps(
                    {'audio': 'sample.wav', 'text': 'Fixture.'}) + '\n')
                talker = torch.nn.Module()
                for name in ('q_proj', 'k_proj', 'v_proj', 'o_proj'):
                    layer = torch.nn.Linear(4, 4, bias=False)
                    with torch.no_grad():
                        layer.weight.fill_(0.25)
                    setattr(talker, name, layer)
                fake_qwen = ModuleType('qwen_tts')
                fake_qwen.Qwen3TTSModel = SimpleNamespace(from_pretrained=lambda *args, **kwargs:
                    SimpleNamespace(processor=object(), model=SimpleNamespace(talker=talker)))
                argv = ['train_lora.py', '--data_dir', tmp, '--output_dir', tmp,
                        '--device', 'cpu', '--epochs', '1', '--lora_r', '2']
                if requested_seed is not None:
                    argv += ['--seed', str(requested_seed)]
                with patch.object(sys, 'argv', argv):
                    args = train_lora.parse_args()
                actual_get_peft_model = peft.get_peft_model
                def record(talker, config):
                    adapted = actual_get_peft_model(talker, config)
                    adapted.save_pretrained(tmp, safe_serialization=True)
                    raise InitializationRecorded()
                torch.manual_seed(ambient_seed)
                with patch.dict(sys.modules, {'qwen_tts': fake_qwen}), \
                     patch.object(train_lora, 'resolve_device', return_value='cpu'), \
                     patch.object(train_lora, 'enable_rocm_optimizations'), \
                     patch.object(torch.cuda, 'is_available', return_value=False), \
                     patch.object(train_lora, 'load_dataset', return_value=([{'text': 'Fixture.'}], 'ref.wav')), \
                     patch.object(peft_lora_model, 'is_bnb_available', return_value=False), \
                     patch.object(peft_lora_model, 'is_bnb_4bit_available', return_value=False), \
                     patch.object(peft, 'get_peft_model', side_effect=record), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(InitializationRecorded):
                        train_lora.train(args)
                artifact = load_file(str(Path(tmp) / 'adapter_model.safetensors'))
                configuration = json.loads((Path(tmp) / 'adapter_config.json').read_text())
                self.assertEqual(2, configuration['r'])
                matrices = {name: tensor for name, tensor in artifact.items() if 'lora_A' in name}
                self.assertEqual(4, len(matrices))
                self.assertTrue(all(torch.isfinite(tensor).all() for tensor in matrices.values()))
                return matrices
        finally:
            torch.random.set_rng_state(before_torch)
            random.setstate(before_python)

    def test_same_requested_seed_produces_identical_saved_matrices(self):
        first = self.get_initial_adapter(17, 101)
        second = self.get_initial_adapter(23, 101)
        self.assertTrue(all(torch.equal(first[k], second[k]) for k in first))

    def test_different_requested_seeds_change_saved_initialization(self):
        first = self.get_initial_adapter(17, 101)
        second = self.get_initial_adapter(17, 202)
        self.assertTrue(any(not torch.equal(first[k], second[k]) for k in first))

    def test_unseeded_runs_keep_ambient_randomness(self):
        first = self.get_initial_adapter(17, None)
        second = self.get_initial_adapter(23, None)
        self.assertTrue(any(not torch.equal(first[k], second[k]) for k in first))
