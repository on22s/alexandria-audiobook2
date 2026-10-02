import hashlib
import tempfile
import unittest
import wave
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

import tts_benchmark
from tts_benchmark import (measure_wav, run_clone_voice_case,
                           run_custom_voice_case, run_design_voice_case,
                           run_lora_voice_case)
import tts_vram_benchmark


class TTSBenchmarkTests(unittest.TestCase):
    def test_measure_wav_reports_duration_throughput_and_audio_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "tone.wav")
            sample_rate = 24000
            times = np.arange(sample_rate, dtype=np.float32) / sample_rate
            samples = (0.25 * np.sin(2 * np.pi * 440 * times) * 32767).astype("<i2")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                output.writeframes(samples.tobytes())
            result = measure_wav(str(path), 0.5)
        self.assertEqual(1.0, result["duration_seconds"])
        self.assertEqual(2.0, result["audio_seconds_per_second"])
        self.assertEqual(24000, result["sample_rate"])
        self.assertGreater(result["rms"], 0.1)
        self.assertEqual(0.0, result["clipping_ratio"])

    def test_measure_wav_rejects_non_pcm16_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "eight-bit.wav")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(1)
                output.setframerate(8000)
                output.writeframes(bytes([128]) * 100)
            with self.assertRaisesRegex(ValueError, "16-bit PCM"):
                measure_wav(str(path), 1.0)

    def test_custom_voice_case_uses_production_engine_call(self):
        class FakeEngine:
            def __init__(self):
                self.loaded = 0
                self.call = None

            def _init_local_custom(self):
                self.loaded += 1

            def generate_custom_voice(self, text, instruct, speaker, config, path):
                self.call = (text, instruct, speaker, config)
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        engine = FakeEngine()
        fixture = {"text": "Hello.", "instruct": "Warmly.", "speaker": "N",
                   "voice": "Ryan", "seed": 3}
        with tempfile.TemporaryDirectory() as tmp:
            metrics = run_custom_voice_case(
                engine, fixture, str(Path(tmp, "out.wav")), load_model=True)
        self.assertEqual(1, engine.loaded)
        self.assertEqual("Hello.", engine.call[0])
        self.assertEqual(0.1, metrics["duration_seconds"])

    def test_utilization_sampling_reports_mean_during_generation(self):
        import time as time_module
        with patch.object(tts_benchmark, "sample_gpu_utilization", return_value=50.0):
            result, mean_utilization = tts_benchmark._run_with_utilization_sampling(
                lambda: time_module.sleep(0.05), poll_interval=0.01)
        self.assertIsNone(result)
        self.assertEqual(50.0, mean_utilization)

    def test_utilization_sampling_returns_none_when_backend_unavailable(self):
        with patch.object(tts_benchmark, "sample_gpu_utilization", return_value=None):
            result, mean_utilization = tts_benchmark._run_with_utilization_sampling(
                lambda: "done", poll_interval=0.01)
        self.assertEqual("done", result)
        self.assertIsNone(mean_utilization)

    def test_custom_voice_case_reports_mean_utilization(self):
        class FakeEngine:
            def _init_local_custom(self):
                pass

            def generate_custom_voice(self, text, instruct, speaker, config, path):
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        with patch.object(tts_benchmark, "sample_gpu_utilization", return_value=55.0), \
             tempfile.TemporaryDirectory() as tmp:
            metrics = run_custom_voice_case(
                FakeEngine(), {"text": "Hello.", "instruct": "", "speaker": "N",
                               "voice": "Ryan", "seed": 1},
                str(Path(tmp, "out.wav")), load_model=True)
        self.assertIn("mean_gpu_utilization_pct", metrics)

    def test_vram_sweep_uses_peak_across_all_sub_batches(self):
        class FakeCuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def reset_peak_memory_stats():
                pass

        class FakeEngine:
            def set_sub_batch_size(self, size):
                pass

            def run_benchmark_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
                self.batch_seed = batch_seed
                return {"completed": [], "failed": [], "peak_vram_gb": 13.83}

            def run_clone_benchmark_batch(self, chunks, voice_config, output_dir,
                                          batch_seed=-1):
                self.clone_batch_seed = batch_seed
                return {"completed": [], "failed": [], "peak_vram_gb": 14.25}

            def _clear_gpu_cache(self):
                pass

        fake_torch = type("FakeTorch", (), {"cuda": FakeCuda})
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {"torch": fake_torch}), \
             patch.object(tts_vram_benchmark, "vram_state", return_value={}):
            results = tts_vram_benchmark.run_sweep(
                engine, {}, [16], tmp, n_chunks_per_run=1)
        self.assertEqual(13.83, results[0]["peak_vram_gb"])
        self.assertEqual(42, engine.batch_seed)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {"torch": fake_torch}), \
             patch.object(tts_vram_benchmark, "vram_state", return_value={}):
            clone_results = tts_vram_benchmark.run_sweep(
                engine, {}, [8], tmp, n_chunks_per_run=1, voice_type="clone")
        self.assertEqual(14.25, clone_results[0]["peak_vram_gb"])
        self.assertEqual(42, engine.clone_batch_seed)

    def test_vram_results_create_nested_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "new", "nested", "results.json")
            tts_vram_benchmark.save_benchmark_results({"ok": True}, str(path))
            self.assertTrue(path.is_file())

    def test_clone_case_separates_prompt_and_generation(self):
        class FakeEngine:
            def _init_local_clone(self):
                pass

            def _get_clone_prompt(self, speaker, config):
                return "prompt"

            def generate_clone_voice(self, text, speaker, config, path):
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "ref.wav")
            ref.write_bytes(b"reference")
            fixture = {"text": "Hello.", "speaker": "CLONE", "seed": 2,
                       "ref_audio": "ref.wav", "ref_text": "Reference.",
                       "ref_audio_sha256": hashlib.sha256(b"reference").hexdigest()}
            metrics = run_clone_voice_case(
                FakeEngine(), fixture, str(Path(tmp, "out.wav")), tmp,
                load_model=True)
        self.assertIn("prompt_build_seconds", metrics)
        self.assertEqual(0.1, metrics["duration_seconds"])

    def test_design_case_moves_preview_into_benchmark_output(self):
        class FakeEngine:
            def _init_local_design(self):
                pass

            def generate_voice_design(self, description, sample_text, seed):
                preview = Path(tmp, "preview.wav")
                with wave.open(str(preview), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return str(preview), 24000

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp, "results", "design.wav")
            metrics = run_design_voice_case(FakeEngine(), {
                "text": "Hello.", "description": "Warm voice.", "seed": 5},
                str(output_path), load_model=True)
            self.assertTrue(output_path.is_file())
            self.assertFalse(Path(tmp, "preview.wav").exists())
        self.assertEqual(0.1, metrics["duration_seconds"])

    def test_lora_case_verifies_adapter_and_uses_production_call(self):
        class FakeEngine:
            def _init_local_lora(self, adapter_path, **kwargs):
                self.loaded = adapter_path
                return "model"

            def _ensure_lora_prompt(self, adapter_path, model, ref_text, **kwargs):
                self.prompt = (adapter_path, model, ref_text)

            def generate_lora_voice(self, text, instruct, voice_data, path):
                self.call = (text, instruct, voice_data)
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "adapter")
            adapter.mkdir()
            hashes = {}
            for name in ("adapter_config.json", "adapter_model.safetensors",
                         "ref_sample.wav", "training_meta.json"):
                content = (b'{"ref_sample_text":"Reference words."}'
                           if name == "training_meta.json" else name.encode())
                Path(adapter, name).write_bytes(content)
                hashes[name] = hashlib.sha256(content).hexdigest()
            engine = FakeEngine()
            fake_torch = type("FakeTorch", (), {"manual_seed": lambda seed: None})
            with patch.dict(sys.modules, {"torch": fake_torch}):
                metrics = run_lora_voice_case(engine, {
                    "text": "Hello.", "instruct": "Warmly.", "seed": 3,
                    "adapter_path": "adapter", "adapter_artifact_sha256": hashes},
                    str(Path(tmp, "out.wav")), tmp, load_model=True)
        self.assertEqual("Hello.", engine.call[0])
        self.assertEqual("Reference words.", engine.prompt[2])
        self.assertIn("prompt_build_seconds", metrics)
        self.assertEqual(0.1, metrics["duration_seconds"])


if __name__ == "__main__":
    unittest.main()


class LoraBenchmarkLoadTimingTests(unittest.TestCase):
    def test_load_timings_follow_actual_adapter_cache_hits_and_switches(self):
        import json
        import threading
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock
        import soundfile as sf
        import tts
        clock = [0.0]
        loads, merges, prompts = [], [], []
        def load_model(*args):
            loads.append(args)
            clock[0] += 5.0
            pcm = (0.2 * np.sin(np.arange(4800) * (0.08 + 0.01 * len(loads)))).astype(np.float32)
            def prompt(**kwargs):
                prompts.append(kwargs)
                clock[0] += 1.0
                return object()
            return SimpleNamespace(model=SimpleNamespace(talker=SimpleNamespace(eval=lambda: None)),
                create_voice_clone_prompt=prompt,
                generate_voice_clone=lambda **kwargs: ([pcm.copy()], 24000))
        def merge(talker, path):
            snapshot = Path(path)
            merges.append((path, (snapshot / "adapter_model.safetensors").read_bytes(),
                           json.loads((snapshot / "training_meta.json").read_text())["ref_sample_text"]))
            clock[0] += 2.0
            return SimpleNamespace(merge_and_unload=lambda: talker)
        fake_torch = ModuleType("torch")
        fake_torch.manual_seed = Mock()
        fake_qwen = ModuleType("qwen_tts")
        fake_qwen.Qwen3TTSModel = object()
        fake_peft = ModuleType("peft")
        fake_peft.PeftModel = SimpleNamespace(from_pretrained=merge)
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        engine._mode = "local"
        engine._local_lora_model = None
        engine._lora_adapter_path = None
        engine._lora_prompt_cache = {}
        engine._model_lock = threading.Lock()
        engine._compile_codec_enabled = False
        engine._max_new_tokens = 100
        engine._resolve_device = lambda: "cpu"
        engine._enable_rocm_optimizations = lambda: None
        engine._clear_gpu_cache = Mock()
        engine._load_model = load_model
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = {}
            inputs = {}
            for name in ("first", "second"):
                adapter = root / name
                adapter.mkdir()
                sf.write(adapter / "ref_sample.wav", np.ones(4800) * 0.1, 24000)
                (adapter / "training_meta.json").write_text(json.dumps({"ref_sample_text": name + " reference"}))
                (adapter / "adapter_model.safetensors").write_bytes(name.encode())
                (adapter / "adapter_config.json").write_text("{}")
                artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapter.iterdir()}
                inputs.update({p: p.read_bytes() for p in adapter.iterdir()})
                fixtures[name] = {"adapter_path": name, "adapter_artifact_sha256": artifacts,
                                  "speaker": "ALICE", "text": "A known line.", "seed": 3}
            with patch.dict(sys.modules, {"torch": fake_torch, "qwen_tts": fake_qwen, "peft": fake_peft}), \
                 patch.object(tts.device_utils, "compute_dtype", return_value="float32"), \
                 patch.object(tts_benchmark.time, "monotonic", side_effect=lambda: clock[0]), \
                 patch.object(tts_benchmark, "_run_with_utilization_sampling", side_effect=lambda fn: (fn(), None)):
                results = []
                for index, (name, flag) in enumerate((("first", False), ("first", False),
                                                     ("second", False), ("second", True))):
                    output = root / f"probe_{index}.wav"
                    result = run_lora_voice_case(engine, fixtures[name], str(output), tmp, load_model=flag)
                    results.append(result)
                    audio, rate = sf.read(output)
                    self.assertEqual(24000, rate)
                    self.assertEqual(4800, len(audio))
                    with wave.open(str(output), "rb") as handle:
                        frames = handle.readframes(handle.getnframes())
                    self.assertEqual(hashlib.sha256(frames).hexdigest(), result["sha256"])
                    self.assertEqual(str(root / name), engine._lora_adapter_path)
            self.assertEqual([7.0, 0.0, 7.0, 0.0], [r["model_and_adapter_load_seconds"] for r in results])
            self.assertEqual([1.0, 0.0, 1.0, 0.0], [r["prompt_build_seconds"] for r in results])
            self.assertEqual(2, len(loads))
            self.assertEqual([b"first", b"second"], [item[1] for item in merges])
            self.assertEqual(["first reference", "second reference"], [item[2] for item in merges])
            self.assertTrue(all(not Path(item[0]).exists() for item in merges))
            self.assertEqual(["first reference", "second reference"], [p["ref_text"] for p in prompts])
            self.assertEqual(results[0]["sha256"], results[1]["sha256"])
            self.assertEqual(results[2]["sha256"], results[3]["sha256"])
            self.assertNotEqual(results[0]["sha256"], results[2]["sha256"])
            engine._clear_gpu_cache.assert_called_once()
            for path, data in inputs.items():
                self.assertEqual(data, path.read_bytes())


class TTSModelFamilyTimingTests(unittest.TestCase):
    def run_cases(self, order, failing_design_loads=0, repetitions=2):
        from types import SimpleNamespace
        clock = [100.0]
        initialized = set()
        loads = []
        failures = [failing_design_loads]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / 'reference.wav'

            def write_wav(path):
                with wave.open(str(path),'wb') as output:
                    output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000)
                    output.writeframes(np.full(2400,1000,dtype='<i2').tobytes())
            write_wav(reference)
            original_reference = reference.read_bytes()

            class Engine:
                def initialize(self, kind):
                    loads.append(kind)
                    if kind in initialized:
                        clock[0] += 0.01
                        return
                    clock[0] += 4
                    if kind == 'design' and failures[0]:
                        failures[0] -= 1
                        raise RuntimeError('design load failed')
                    initialized.add(kind)
                def _init_local_custom(self):self.initialize('custom')
                def _init_local_clone(self):self.initialize('clone')
                def _init_local_design(self):self.initialize('design')
                def generate(self, kind, path):
                    if kind not in initialized:
                        self.initialize(kind)
                    clock[0] += 2
                    write_wav(path)
                    return True
                def generate_custom_voice(self,text,instruct,speaker,config,path):
                    return self.generate('custom',path)
                def _get_clone_prompt(self,speaker,config):
                    self.assertion = config[speaker]['ref_audio']
                    clock[0] += 1
                    return 'CPU prompt stand-in'
                def generate_clone_voice(self,text,speaker,config,path):
                    return self.generate('clone',path)
                def generate_voice_design(self,description,sample_text,seed):
                    path = root / 'preview.wav'
                    self.generate('design',path)
                    return str(path),24000
            engine = Engine()
            fixtures = []
            for number,kind in enumerate(order):
                fixture = {'id':str(number)+'-'+kind,'text':'A measured fixture.',
                           'speaker':'ALICE','voice':'Ryan','seed':42,
                           'description':'A clear voice.','ref_audio':'reference.wav',
                           'ref_text':'Reference text.',
                           'ref_audio_sha256':hashlib.sha256(original_reference).hexdigest()}
                if kind != 'implicit-custom':
                    fixture['voice_type'] = kind
                fixtures.append(fixture)
            payload = {'tts':{},'fixtures':fixtures,'repetitions':repetitions}
            with patch.object(tts_benchmark,'TTSEngine',return_value=engine), \
                 patch.object(tts_benchmark,'__file__',str(root / 'app/tts_benchmark.py')), \
                 patch.object(tts_benchmark,'time',SimpleNamespace(monotonic=lambda:clock[0])), \
                 patch.object(tts_benchmark,'sample_gpu_utilization',return_value=None):
                cases = tts_benchmark.execute_payload(payload,str(root / 'output'))
            self.assertEqual(original_reference,reference.read_bytes())
            for case in cases:
                output = root / 'output' / f"{case['fixture_id']}-{case['repetition']}.wav"
                self.assertEqual(case['status']=='passed',output.is_file())
                if case['status']=='passed':
                    reread = tts_benchmark.measure_wav(str(output),2)
                    self.assertEqual(reread['sha256'],case['metrics']['sha256'])
                    self.assertEqual(0.1,case['metrics']['duration_seconds'])
                    self.assertEqual(2,case['metrics']['elapsed_seconds'],
                                     'model initialization must not be charged to generation')
                    self.assertEqual(0.05,case['metrics']['audio_seconds_per_second'])
            self.assertFalse((root / 'preview.wav').exists())
            return cases,loads

    def test_independent_model_families_have_cold_then_warm_timings_in_any_fixture_order(self):
        import itertools
        for kinds in itertools.permutations(('custom','clone','design')):
            with self.subTest(order=kinds):
                cases,loads = self.run_cases((*kinds,'implicit-custom','unrecognized'))
                self.assertTrue(all(case['status']=='passed' for case in cases))
                cold = set()
                for case in cases:
                    kind = case['fixture_id'].split('-',1)[1]
                    if kind in ('implicit-custom','unrecognized'):
                        kind = 'custom'
                    self.assertEqual(0 if kind in cold else 4,
                                     case['metrics']['model_load_seconds'])
                    if kind == 'clone':
                        self.assertEqual(1,case['metrics']['prompt_build_seconds'])
                    cold.add(kind)
                self.assertCountEqual(['custom','clone','design'],loads)

    def test_failed_model_initialization_does_not_mark_that_family_warm(self):
        cases,loads = self.run_cases(('custom','design'),failing_design_loads=1,repetitions=3)
        designs = [case for case in cases if case['fixture_id']=='1-design']
        self.assertEqual(['failed','passed','passed'],[case['status'] for case in designs])
        self.assertEqual('design load failed',designs[0]['error'])
        self.assertEqual({},designs[0]['metrics'])
        self.assertEqual(4,designs[1]['metrics']['model_load_seconds'])
        self.assertEqual(0,designs[2]['metrics']['model_load_seconds'])
        self.assertEqual(['custom','design','design'],loads)
