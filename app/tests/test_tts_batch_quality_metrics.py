"""The TTS experiment must retain an early peak across production counter resets."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class TtsBatchQualityMetricsTests(unittest.TestCase):
    def test_earlier_allocation_peak_survives_later_generation_reset(self):
        app = Path(__file__).parent.parent
        with patch.object(sys, "argv", ["benchmark", "--app", str(app)]):
            spec = importlib.util.spec_from_file_location(
                "tts_batch_quality_test", app / "experiments/tts_batch_quality_20260929.py")
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
                # Warmup has a larger peak and must be excluded; the first timed
                # call has9GB, the next resets the Torch counter and has2GB.
                peak = {1: 20, 2: 9, 3: 2}[meter["calls"]]
                meter["current"] = peak * 1e9
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
        sf = SimpleNamespace(info=lambda path: SimpleNamespace(duration=2))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "scores.json")
            with patch.object(sys, "argv", ["benchmark", "--app", str(app),
                 "--workers", "1", "--lines", "2", "--out", str(output)]), \
                 patch.dict(sys.modules, {"torch": torch, "soundfile": sf}), \
                 patch("tts.TTSEngine", Engine), \
                 patch("config_settings.load_app_config", return_value={"tts": {}}), \
                 patch("experiments.asr_backends.run_transformers_whisper", return_value=("text", [])), \
                 patch("experiments.asr_backends.word_error_rate", return_value=0), \
                 patch.object(producer.vb, "gpu_name", return_value="test GPU"):
                producer.main()
            render = json.loads(output.read_text())["render"][0]
            self.assertEqual(9, render["torch_max_allocated_gb"])
            self.assertEqual(3, render["torch_last_sub_batch_peak_reserved_gb"])
            self.assertEqual(2, render["clips"])
            self.assertEqual(0, render["failed"])
            self.assertEqual(4, render["audio_s"])
            self.assertEqual(3, meter["calls"])
