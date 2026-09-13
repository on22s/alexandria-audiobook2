"""The LoRA path merges the adapter into the talker instead of serving it
through PEFT's per-step forward hook (goal 4.1: 1.25x -> 0.835x realtime)."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tts as tts_module


class _Talker:
    """Stands in for the base talker; `config` is what the VRAM estimator reads."""
    config = object()

    def __init__(self):
        self.eval_calls = 0

    def eval(self):
        self.eval_calls += 1
        return self


class _PeftWrapper:
    """A PEFT wrapper that records whether it was merged. Its eval() must NOT
    be the one the engine calls: only the merged module should be served."""
    instances = []

    def __init__(self, base, adapter_path):
        self.base, self.adapter_path = base, adapter_path
        self.merged = False
        self.eval_calls = 0
        _PeftWrapper.instances.append(self)

    @classmethod
    def from_pretrained(cls, base, adapter_path):
        return cls(base, adapter_path)

    def merge_and_unload(self):
        self.merged = True
        return self.base

    def eval(self):
        self.eval_calls += 1
        return self


def _fake_model():
    return types.SimpleNamespace(model=types.SimpleNamespace(talker=_Talker()))


class LoraMergeTests(unittest.TestCase):
    def setUp(self):
        _PeftWrapper.instances.clear()
        self.fake_qwen = types.SimpleNamespace(Qwen3TTSModel=object())
        self.fake_peft = types.SimpleNamespace(PeftModel=_PeftWrapper)
        # CI has no torch; and a real torch first imported inside patch.dict
        # would be evicted on restore and fail to re-initialise.
        self.fake_torch = types.SimpleNamespace(bfloat16="bf16", float32="f32")

    def _load(self, adapter="lora_models/some_voice"):
        engine = tts_module.TTSEngine({"tts": {"mode": "local"}})
        with patch.dict(sys.modules, {"torch": self.fake_torch, "qwen_tts": self.fake_qwen,
                                  "peft": self.fake_peft}), \
             patch.object(tts_module.TTSEngine, "_load_model",
                          staticmethod(lambda cls, model_id, kw: _fake_model())), \
             patch.object(engine, "_enable_rocm_optimizations"), \
             patch.object(engine, "_resolve_device", return_value="cpu"):
            return engine, engine._init_local_lora(adapter)

    def test_served_talker_is_the_merged_module_not_the_peft_wrapper(self):
        engine, model = self._load()
        [wrapper] = _PeftWrapper.instances
        self.assertTrue(wrapper.merged)
        self.assertEqual("lora_models/some_voice", wrapper.adapter_path)
        self.assertIs(model.model.talker, wrapper.base)
        self.assertIsInstance(model.model.talker, _Talker)
        self.assertEqual(1, model.model.talker.eval_calls)
        self.assertEqual(0, wrapper.eval_calls)
        self.assertIs(engine._local_lora_model, model)

    def test_merged_talker_still_exposes_config_for_the_vram_estimator(self):
        _, model = self._load()
        self.assertIs(_Talker.config, model.model.talker.config)


if __name__ == "__main__":
    unittest.main()
