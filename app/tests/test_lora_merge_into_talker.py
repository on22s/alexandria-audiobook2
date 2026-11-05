"""LoRA serving retains fp32 adapters on bf16 bases and the fp32 merge path."""
import contextlib
import io
import sys
import types
import unittest
import tempfile
from tests.test_support import write_test_adapter
import numpy as np
import soundfile as sf
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tts as tts_module


class _Talker:
    """Stands in for the base talker; `config` is what the VRAM estimator reads."""
    config = object()

    def __init__(self):
        self.eval_calls = 0
        self.training = True

    def train(self, mode=True):
        self.training = mode
        return self

    def eval(self):
        self.eval_calls += 1
        return self.train(False)


class _PeftWrapper:
    """Record adapter loading, merging, and evaluation of the served module."""
    instances = []

    def __init__(self, base, adapter_path, **kwargs):
        self.base, self.adapter_path = base, adapter_path
        self.kwargs = kwargs
        self.autocast_adapter_dtype = kwargs.get("autocast_adapter_dtype")
        self.merged = False
        self.eval_calls = 0
        self.training = True
        _PeftWrapper.instances.append(self)

    @classmethod
    def from_pretrained(cls, base, adapter_path, **kwargs):
        return cls(base, adapter_path, **kwargs)

    @property
    def config(self):
        return self.base.config

    def merge_and_unload(self):
        self.merged = True
        return self.base

    def eval(self):
        self.eval_calls += 1
        self.training = False
        self.base.train(False)
        return self


def _fake_model(recovery=False):
    model = types.SimpleNamespace(model=types.SimpleNamespace(talker=_Talker()))
    if recovery:
        model._aa2_recovery_loaded_model = object()
    return model


class LoraMergeTests(unittest.TestCase):
    def setUp(self):
        _PeftWrapper.instances.clear()
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.adapter=str(Path(temporary.name)/"some_voice")
        write_test_adapter(self.adapter)
        sf.write(str(Path(self.adapter)/"ref_sample.wav"),np.full(2400,.1),24000)
        self.fake_qwen = types.SimpleNamespace(Qwen3TTSModel=object())
        self.fake_peft = types.SimpleNamespace(PeftModel=_PeftWrapper)
        self.fake_recovery_serving = types.SimpleNamespace(
            get_native_lora_merge_sha256=lambda: "merge-sha256")
        self.fake_recovery_identity = types.SimpleNamespace(
            RecoveryIdentityUnavailable=type(
                "RecoveryIdentityUnavailable", (Exception,), {}))
        # CI has no torch; and a real torch first imported inside patch.dict
        # would be evicted on restore and fail to re-initialise.
        self.fake_torch = types.SimpleNamespace(bfloat16="bf16", float32="f32")

    def _load(self, adapter=None, *, device="cpu", dtype=None, recovery=False):
        adapter = self.adapter if adapter is None else adapter
        dtype = self.fake_torch.float32 if dtype is None else dtype
        engine = tts_module.TTSEngine({"tts": {"mode": "local"}})
        def load_model(cls, model_id, kw, *, release_for_oom=None):
            self.assertTrue(callable(release_for_oom))
            return _fake_model(recovery=recovery)
        with patch.dict(sys.modules, {"torch": self.fake_torch, "qwen_tts": self.fake_qwen,
                                  "peft": self.fake_peft,
                                  "synthesis_recovery_serving": self.fake_recovery_serving,
                                  "synthesis_recovery_identity": self.fake_recovery_identity}), \
             patch.object(tts_module.TTSEngine, "_load_model",
                          staticmethod(load_model)), \
             patch.object(engine, "_enable_rocm_optimizations"), \
             patch.object(engine, "_resolve_device", return_value=device), \
             patch.object(tts_module.device_utils, "compute_dtype", return_value=dtype), \
             patch.object(tts_module.device_utils, "get_attention_load_kwargs", return_value={}), \
             patch.object(engine, "_release_inactive_local_models"):
            return engine, engine._init_local_lora(adapter)

    def test_served_talker_is_the_merged_module_not_the_peft_wrapper(self):
        engine, model = self._load()
        [wrapper] = _PeftWrapper.instances
        self.assertTrue(wrapper.merged)
        self.assertEqual(self.adapter, engine._lora_adapter_path)
        self.assertNotEqual(self.adapter, wrapper.adapter_path)
        self.assertFalse(Path(wrapper.adapter_path).exists())
        self.assertIs(model.model.talker, wrapper.base)
        self.assertIsInstance(model.model.talker, _Talker)
        self.assertEqual(1, model.model.talker.eval_calls)
        self.assertEqual(0, wrapper.eval_calls)
        self.assertIs(engine._local_lora_model, model)

    def test_bfloat16_serves_unmerged_peft_with_autocast_and_no_merge_stamp(self):
        for device in ("cuda", "cuda:0"):
            with self.subTest(device=device):
                _PeftWrapper.instances.clear()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    engine, model = self._load(
                        device=device, dtype=self.fake_torch.bfloat16, recovery=True)
                [wrapper] = _PeftWrapper.instances
                self.assertFalse(wrapper.merged)
                self.assertIs(True, wrapper.autocast_adapter_dtype)
                self.assertIs(model.model.talker, wrapper)
                self.assertEqual(1, wrapper.eval_calls)
                self.assertFalse(wrapper.training)
                self.assertFalse(wrapper.base.training)
                self.assertIs(True, model._aa2_recovery_lora_unmerged)
                self.assertIn("recovery certificates and window reuse are unavailable",
                              output.getvalue())
                self.assertIs(_Talker.config, model.model.talker.config)
                self.assertIs(engine._local_lora_model, model)
                self.assertEqual(self.adapter, engine._lora_adapter_path)
                self.assertFalse(hasattr(model, "_aa2_recovery_lora_merge_sha256"))
                self.assertIs(model, engine._init_local_lora(self.adapter))
                self.assertEqual(1, len(_PeftWrapper.instances))
                self.assertFalse(Path(wrapper.adapter_path).exists())

    def test_float32_still_merges_and_stamps_recovery_identity(self):
        engine, model = self._load(dtype=self.fake_torch.float32, recovery=True)
        [wrapper] = _PeftWrapper.instances
        self.assertTrue(wrapper.merged)
        self.assertIsNone(wrapper.autocast_adapter_dtype)
        self.assertIs(model.model.talker, wrapper.base)
        self.assertEqual(1, model.model.talker.eval_calls)
        self.assertEqual(0, wrapper.eval_calls)
        self.assertEqual("merge-sha256", model._aa2_recovery_lora_merge_sha256)
        self.assertFalse(hasattr(model, "_aa2_recovery_lora_unmerged"))

    def test_observed_serving_identity_tracks_loader_without_runtime_queries(self):
        self.fake_torch.float32 = "torch.float32"
        self.fake_torch.bfloat16 = "torch.bfloat16"
        for device, dtype, mode in (
                ("cpu", self.fake_torch.float32, "merged"),
                ("cuda", self.fake_torch.bfloat16, "unmerged"),
                ("cuda:1", self.fake_torch.bfloat16, "unmerged"),
                ("cuda", self.fake_torch.float32, "merged")):
            with self.subTest(device=device, dtype=dtype):
                engine, model = self._load(device=device, dtype=dtype)
                read_identity = getattr(engine, "get_lora_serving_identity", lambda: None)
                with patch.object(engine, "_resolve_device", side_effect=AssertionError("runtime query")), \
                     patch.object(tts_module.device_utils, "compute_dtype",
                                  side_effect=AssertionError("dtype query")):
                    identity = read_identity()
                self.assertEqual({"policy_version": 1, "device": device,
                                  "dtype": dtype.removeprefix("torch."), "merge_mode": mode}, identity)
                identity["merge_mode"] = "changed copy"
                self.assertEqual(mode, read_identity()["merge_mode"])
                engine._local_lora_model = None
                self.assertIsNone(read_identity())

    def test_serving_identity_is_unknown_without_waiting_for_model_load(self):
        import threading
        engine, model = self._load()
        observed = []
        reader = threading.Thread(target=lambda: observed.append(engine.get_lora_serving_identity()))
        with engine._model_lock:
            reader.start()
            reader.join(timeout=1)
            blocked = reader.is_alive()
        reader.join(timeout=2)
        self.assertFalse(blocked, "metadata reads must not wait for a model load")
        self.assertEqual([None], observed)

    def test_merged_talker_still_exposes_config_for_the_vram_estimator(self):
        _, model = self._load()
        self.assertIs(_Talker.config, model.model.talker.config)


if __name__ == "__main__":
    unittest.main()
