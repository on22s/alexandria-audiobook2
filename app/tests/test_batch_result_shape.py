"""Batch backends share one result shape, and an unmeasured VRAM reading is never a fit.

Finding 2: external batches must resolve output paths through the recovery-aware
helper like every other batch site. Finding 3: every batch backend reports
peak_vram_gb, and a None (no local reading) is rendered as N/A with an explicit
non-fitting verdict instead of a measured-looking 0.00 GB.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

import tts
import tts_vram_benchmark as benchmark


class VramNoneRenderingTests(unittest.TestCase):
    def test_none_peak_renders_na_and_is_never_judged_to_fit(self):
        rows = [{"sub_batch_max_items": 4, "peak_vram_gb": None,
                 "rtf": 2.0, "failed": 0}]
        with patch.object(benchmark, "gpu_name", return_value="CPU fixture"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            benchmark.print_summary(rows, [], 8, 16)
        text = output.getvalue()
        self.assertIn("peak=N/A", text)
        self.assertIn("[NOT-MEASURED]", text)
        self.assertNotIn("[OK", text)
        self.assertNotIn("OOM-RISK", text)

    def test_measured_peak_still_renders_and_fits_as_before(self):
        rows = [{"sub_batch_max_items": 4, "peak_vram_gb": 12.0,
                 "rtf": 2.0, "failed": 0}]
        with patch.object(benchmark, "gpu_name", return_value="CPU fixture"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            benchmark.print_summary(rows, [], 8, 16)
        text = output.getvalue()
        self.assertIn("peak=12.00GB", text)
        self.assertIn("[OK", text)
        self.assertNotIn("[NOT-MEASURED]", text)

    def test_compiled_none_peak_renders_na_in_summary_table(self):
        measured = [{"sub_batch_max_items": 4, "peak_vram_gb": 12.0,
                     "rtf": 2.0, "failed": 0}]
        compiled = [{"sub_batch_max_items": 4, "peak_vram_gb": None,
                     "rtf": 2.0, "failed": 0}]
        with patch.object(benchmark, "gpu_name", return_value="CPU fixture"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            benchmark.print_summary(measured, compiled, 8, 16)
        text = output.getvalue()
        column_lines = [line for line in text.splitlines() if "12.00" in line or "N/A" in line]
        self.assertTrue(any("N/A" in line for line in column_lines))
        tier = [line for line in text.splitlines() if "max_items=" in line]
        self.assertEqual(2, len(tier))
        self.assertIn("[NOT-MEASURED]", tier[1])
        self.assertNotIn("[OK", tier[1])

    def test_run_sweep_keeps_none_as_null_in_artifact_and_prints_na(self):
        engine = SimpleNamespace(
            set_sub_batch_size=Mock(),
            run_benchmark_batch=Mock(return_value={
                "completed": [], "failed": [], "peak_vram_gb": None}),
            _clear_gpu_cache=Mock())
        fake_torch = ModuleType("torch")
        fake_torch.cuda = SimpleNamespace(is_available=lambda: False,
                                          reset_peak_memory_stats=Mock())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(sys.modules, {"torch": fake_torch}), \
                patch.object(benchmark, "make_chunks", return_value=[]), \
                patch.object(benchmark, "vram_state", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as logs:
            results = benchmark.run_sweep(engine, {}, [4], tmp)
        self.assertIsNone(results[0]["peak_vram_gb"])
        self.assertIn('"peak_vram_gb": null', json.dumps(results, indent=2))
        self.assertIn("peak_VRAM=N/A", logs.getvalue())


class _FakeModel:
    device = "cuda:1"

    def generate_custom_voice(self, **kwargs):
        return [np.full(240, 0.1, dtype="float32") for _ in kwargs["text"]], 24000

    def generate_voice_clone(self, **kwargs):
        return [np.full(240, 0.1, dtype="float32") for _ in kwargs["text"]], 24000

    def _tokenize_texts(self, texts):
        return [object() for _ in texts]


def _local_engine(peaks, available=True):
    model = _FakeModel()
    reference = SimpleNamespace(ref_code=np.zeros((4,)), ref_text="reference line")
    engine = tts.TTSEngine.__new__(tts.TTSEngine)
    engine._init_local_custom = Mock(return_value=model)
    engine._init_local_clone = Mock(return_value=model)
    engine._ensure_local_lora_generation = Mock(return_value=(model, [reference]))
    engine.ensure_custom_warmup = Mock()
    engine._get_clone_prompt = Mock(return_value=[reference])
    engine._clear_gpu_cache = Mock()
    engine._estimate_max_batch_size = Mock(return_value=1)
    engine._language = "English"
    engine._max_new_tokens = 20
    fake_torch = ModuleType("torch")
    peak_values = iter(peaks)
    fake_torch.cuda = SimpleNamespace(
        is_available=lambda: available,
        reset_peak_memory_stats=Mock(),
        max_memory_allocated=Mock(side_effect=lambda device: next(peak_values) * 1e9))
    fake_torch.manual_seed = Mock()
    return engine, fake_torch


class BatchResultShapeTests(unittest.TestCase):
    def test_all_four_batch_backends_report_peak_vram_gb_key(self):
        chunks = [{"speaker": "A", "text": "line", "index": 0}]
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "adapter"
            adapter.mkdir()
            custom_config = {"A": {"type": "custom", "voice": "Ryan"}}
            clone_config = {"A": {"type": "clone", "ref_audio": str(adapter),
                                  "ref_text": "reference line"}}
            lora_config = {"A": {"type": "lora", "adapter_path": str(adapter)}}
            engine, fake_torch = _local_engine([1, 1, 1])
            for name, method, config in (
                    ("custom", engine._local_batch_custom, custom_config),
                    ("clone", engine._local_batch_clone, clone_config),
                    ("lora", engine._local_batch_lora, lora_config)):
                with self.subTest(backend=name), \
                        patch.dict(sys.modules, {"torch": fake_torch}), \
                        patch.object(tts, "get_resolved_adapter_path", side_effect=lambda p: p), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = method(chunks, config, tmp)
                self.assertIn("peak_vram_gb", result)
                self.assertEqual([0], result["completed"])
            external = tts.TTSEngine({"tts": {"mode": "external", "url": "http://fixture",
                                             "parallel_workers": 1}})
            result = external._external_batch([], {}, tmp, "custom")
            self.assertIn("peak_vram_gb", result)
            self.assertIsNone(result["peak_vram_gb"])

    def test_lora_batch_measures_peak_across_sub_batches(self):
        chunks = [{"speaker": "A", "text": "a", "index": 0},
                  {"speaker": "A", "text": "bb", "index": 1},
                  {"speaker": "A", "text": "ccc", "index": 2}]
        config = {"A": {"type": "lora", "adapter_path": "builtin_fixture"}}
        for available, expected, expected_reset in ((True, 3.1, 3), (False, 0, 0)):
            with self.subTest(available=available), tempfile.TemporaryDirectory() as tmp:
                engine, fake_torch = _local_engine([1, 3.1, 2], available)
                engine._build_sub_batches = Mock(return_value=[(0, 1), (1, 2), (2, 3)])
                logs = io.StringIO()
                with patch.dict(sys.modules, {"torch": fake_torch}), \
                        patch.object(tts, "get_resolved_adapter_path", side_effect=lambda p: p), \
                        patch.object(tts.os.path, "isdir", return_value=True), \
                        contextlib.redirect_stdout(logs):
                    result = engine._local_batch_lora(chunks, config, tmp, batch_seed=41)
                self.assertEqual([], result["failed"], logs.getvalue())
                self.assertEqual(expected, result["peak_vram_gb"])
                self.assertEqual(expected_reset, fake_torch.cuda.reset_peak_memory_stats.call_count)
                self.assertEqual(expected_reset, fake_torch.cuda.max_memory_allocated.call_count)
                self.assertEqual(available, "Peak VRAM lora" in logs.getvalue())


class ExternalBatchOutputPathTests(unittest.TestCase):
    def test_external_batch_resolves_output_through_recovery_helper(self):
        engine = tts.TTSEngine({"tts": {"mode": "external", "url": "http://fixture",
                                       "parallel_workers": 1}})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved = root / "registered.wav"

            def generate(text, instruct, speaker, config, path, **kwargs):
                sf.write(path, np.full(240, 0.2), 24000, format="WAV")
                return True

            with patch.object(engine, "_external_generate_custom", side_effect=generate), \
                    patch.object(tts, "get_synthesis_batch_output_path",
                                 return_value=str(resolved)) as resolve, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = engine._external_batch(
                    [{"index": 5, "speaker": "A", "text": "line"}],
                    {"A": {"voice": "Ryan"}}, str(root), "custom")
            resolve.assert_called_once_with(str(root), 5)
            self.assertEqual([5], result["completed"])
            self.assertIsNone(result["peak_vram_gb"])
            self.assertTrue(resolved.exists())


class BatchQualityNoneTests(unittest.TestCase):
    def test_experiment_skips_none_peak_without_raising(self):
        app = Path(__file__).parent.parent
        with patch.object(sys, "argv", ["benchmark", "--app", str(app)]):
            spec = importlib.util.spec_from_file_location(
                "tts_batch_quality_none_test", app / "experiments/tts_batch_quality_20260929.py")
            producer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(producer)
        meter = {"current": 0, "calls": 0}

        class Engine:
            def __init__(self, config):
                pass

            def _init_local_custom(self):
                self._local_custom_model = object()

            def ensure_custom_warmup(self, model):
                pass

            def set_sub_batch_size(self, size):
                pass

            def run_benchmark_batch(self, chunks, voices, directory, batch_seed):
                meter["calls"] += 1
                # Warmup (20), then an unmeasured batch (None), then 9 GB.
                peak = {1: 20, 2: None, 3: 9}[meter["calls"]]
                meter["current"] = (peak or 0) * 1e9
                for chunk in chunks:
                    Path(directory, "temp_batch_%d.wav" % chunk["index"]).write_bytes(b"clip")
                return {"completed": [c["index"] for c in chunks],
                        "failed": [], "peak_vram_gb": peak}

        cuda = SimpleNamespace(synchronize=lambda: None,
                               reset_peak_memory_stats=lambda: meter.update(current=0),
                               mem_get_info=lambda: (10e9, 32e9),
                               max_memory_allocated=lambda: meter["current"],
                               max_memory_reserved=lambda: 3e9, empty_cache=lambda: None)
        torch = SimpleNamespace(cuda=cuda)
        sf_module = SimpleNamespace(info=lambda path: SimpleNamespace(duration=2))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "scores.json")
            with patch.object(sys, "argv", ["benchmark", "--app", str(app),
                    "--workers", "1", "--lines", "2", "--out", str(output)]), \
                    patch.dict(sys.modules, {"torch": torch, "soundfile": sf_module}), \
                    patch("tts.TTSEngine", Engine), \
                    patch("config_settings.load_app_config", return_value={"tts": {}}), \
                    patch("experiments.asr_backends.run_transformers_whisper",
                          return_value=("text", [])), \
                    patch("experiments.asr_backends.word_error_rate", return_value=0), \
                    patch.object(producer.vb, "gpu_name", return_value="test GPU"):
                producer.main()
            render = json.loads(output.read_text())["render"][0]
        self.assertEqual(9, render["torch_max_allocated_gb"])


if __name__ == "__main__":
    unittest.main()
