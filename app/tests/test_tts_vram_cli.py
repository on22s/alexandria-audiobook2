"""CLI rejection must precede costly model setup; fixtures use CPU stubs."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tts_vram_benchmark as benchmark


class VramCliValidationTests(unittest.TestCase):
    def test_real_summary_is_persisted_beside_results_and_failed_replacement_keeps_prior_bytes(self):
        engine = SimpleNamespace(_local_custom_model='fixture',
                                 _init_local_custom=Mock(), ensure_custom_warmup=Mock())
        rows = [{'sub_batch_max_items': 4, 'peak_vram_gb': 1, 'rtf': 2, 'failed': 0}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / 'nested' / 'result.json'
            summary = output.parent / 'benchmark_summary.txt'
            with patch.object(benchmark, 'APP_DIR', tmp), \
                    patch.object(sys, 'argv', ['tts_vram_benchmark.py', '--out', 'nested/result.json']), \
                    patch.object(benchmark, 'load_app_config', return_value={'tts': {}}), \
                    patch('tts.TTSEngine', return_value=engine), \
                    patch('experiments.gpu_guard.acquire_gpu_lock', return_value=None), \
                    patch('experiments.gpu_guard.release_gpu_lock') as release, \
                    patch.object(benchmark, 'vram_state', return_value={'allocated_gb': 0, 'free_gb': 8, 'total_gb': 8}), \
                    patch.object(benchmark, 'gpu_name', return_value='Fixture GPU'), \
                    patch.object(benchmark, 'run_sweep', return_value=rows), \
                    contextlib.redirect_stdout(io.StringIO()) as terminal:
                benchmark.main()
                self.assertTrue(summary.exists())
                text = summary.read_text()
                self.assertIn('BENCHMARK SUMMARY', text)
                self.assertIn('Tier table recommendation', text)
                self.assertIn('Fixture GPU', text)
                self.assertIn('max_items=  4', text)
                self.assertIn(text, terminal.getvalue())
                self.assertEqual(rows, json.loads(output.read_text())['baseline'])
                old = summary.read_bytes()
                rows[0]['rtf'] = 3
                replace = benchmark.os.replace
                def refuse_summary(src, dst):
                    if Path(dst) == summary:
                        raise PermissionError('synthetic summary publication refusal')
                    return replace(src, dst)
                with patch.object(benchmark.os, 'replace', side_effect=refuse_summary):
                    with self.assertRaisesRegex(PermissionError, 'summary publication refusal'):
                        benchmark.main()
                self.assertEqual(old, summary.read_bytes())
                self.assertEqual(2, release.call_count)
                self.assertEqual({'result.json', 'benchmark_summary.txt'},
                                 {path.name for path in output.parent.iterdir()})

    def test_invalid_sizes_chunks_and_outputs_stop_before_config_or_engine(self):
        cases = (['--sizes', '0'], ['--sizes', '-1'], ['--sizes', '4', '0'],
                 ['--chunks', '0'], ['--chunks', '-3'],
                 ['--out', '../outside.json'], ['--out', 'existing'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'existing').mkdir()
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    before = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
                    with patch.object(benchmark, 'APP_DIR', tmp), \
                         patch.object(sys, 'argv', ['tts_vram_benchmark.py', *arguments]), \
                         patch.object(benchmark, 'load_app_config', side_effect=AssertionError('invalid CLI reached config setup')) as load, \
                         patch('tts.TTSEngine', side_effect=AssertionError('invalid CLI loaded engine')) as engine, \
                         contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            benchmark.main()
                    self.assertEqual(2, raised.exception.code)
                    load.assert_not_called()
                    engine.assert_not_called()
                    self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob('*')))

    def test_valid_sweep_keeps_options_and_publishes_nested_result(self):
        stored = {'mode': 'external', 'device': 'cpu', 'compile_codec': True,
                  'sub_batch_ratio': 5.0, 'sub_batch_min_size': 4}
        engine = SimpleNamespace(_local_custom_model='cpu fixture',
                                 _init_local_custom=Mock(), ensure_custom_warmup=Mock())
        results = [{'sub_batch_max_items': 1}, {'sub_batch_max_items': 2}]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'nested/result.json'
            with patch.object(benchmark, 'APP_DIR', tmp), \
                 patch.object(sys, 'argv', ['tts_vram_benchmark.py', '--sizes', '1', '2',
                     '--chunks', '3', '--out', 'nested/result.json']), \
                 patch.object(benchmark, 'load_app_config', return_value={'tts': stored}), \
                 patch('tts.TTSEngine', return_value=engine) as create, \
                 patch('experiments.gpu_guard.acquire_gpu_lock', return_value=None), \
                 patch.object(benchmark, 'vram_state', return_value=None), \
                 patch.object(benchmark, 'gpu_name', return_value='CPU fixture'), \
                 patch.object(benchmark, 'run_sweep', return_value=results) as sweep, \
                 patch.object(benchmark, 'print_summary'), \
                 contextlib.redirect_stdout(io.StringIO()):
                benchmark.main()
            config = create.call_args.args[0]['tts']
            self.assertEqual('local', config['mode'])
            self.assertFalse(config['compile_codec'])
            self.assertEqual(1, config['sub_batch_max_items'])
            self.assertEqual(5.0, config['sub_batch_ratio'])
            self.assertEqual(4, config['sub_batch_min_size'])
            self.assertEqual('external', stored['mode'])
            self.assertTrue(stored['compile_codec'])
            sweep.assert_called_once()
            self.assertEqual([1, 2], sweep.call_args.args[2])
            self.assertEqual(3, sweep.call_args.args[4])
            self.assertEqual('custom', sweep.call_args.args[5])
            saved = json.loads(output.read_bytes())
            self.assertEqual(results, saved['baseline'])
            self.assertEqual([], saved['compiled'])
            self.assertFalse(saved['compile_tested'])
            self.assertEqual('CPU fixture', saved['gpu'])
            self.assertEqual({output, output.parent / 'benchmark_summary.txt'},
                             {p for p in Path(tmp).rglob('*') if p.is_file()})


class VramSummaryHeadroomTests(unittest.TestCase):
    def test_absolute_peak_counts_resident_model_once_and_preserves_headroom_margin(self):
        # The engine reports max_memory_allocated/1e9, including its resident
        # model. These hand-calculated cases preserve the existing 85% margin.
        for model, total, peak, expected in ((14, 24, 16, "OK"),
                (8, 16, 14.8, "OK"), (8, 16, 14.81, "OOM-RISK"),
                (8, 16, 16, "OOM-RISK"), (0, 16, 13.6, "OK"),
                (0, 16, 13.61, "OOM-RISK")):
            with self.subTest(model=model, total=total, peak=peak):
                rows = [{"sub_batch_max_items": 4, "peak_vram_gb": peak,
                         "rtf": 2.0, "failed": 0}]
                original = json.dumps(rows)
                with patch.object(benchmark, "gpu_name", return_value="Controlled CPU fixture"), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    benchmark.print_summary(rows, [], model, total)
                recommendations = [line for line in output.getvalue().splitlines()
                                   if "max_items=" in line]
                self.assertEqual(1, len(recommendations))
                self.assertIn("[" + expected, recommendations[0])
                self.assertIn(f"peak={peak:.2f}GB", recommendations[0])
                self.assertEqual(original, json.dumps(rows))

    def test_multiple_recommendations_retain_peak_and_both_timing_columns(self):
        rows = [{"sub_batch_max_items": size, "peak_vram_gb": peak,
                 "rtf": rtf, "failed": 0} for size, peak, rtf in
                ((4, 10, 2.5), (8, 14.9, 3.5))]
        compiled = [{**row, "rtf": row["rtf"] + 1} for row in rows]
        with patch.object(benchmark, "gpu_name", return_value="Controlled CPU fixture"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            benchmark.print_summary(rows, compiled, 8, 16)
        text = output.getvalue()
        self.assertIn("RTF (compiled)", text)
        self.assertIn("3.50", text)
        self.assertIn("4.50", text)
        recommendations = [line for line in text.splitlines() if "max_items=" in line]
        self.assertEqual(4, len(recommendations))
        self.assertIn("[OK", recommendations[0])
        self.assertIn("[OOM-RISK]", recommendations[1])
        self.assertIn("[OK", recommendations[2])
        self.assertIn("[OOM-RISK]", recommendations[3])
        self.assertIn("RTF=3.50x RT", recommendations[2])
        self.assertIn("RTF=4.50x RT", recommendations[3])


class VramCliLeaseTests(unittest.TestCase):
    def test_busy_kernel_lease_refuses_before_loading_engine(self):
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "gpu.lock"
            with open(lock, "a") as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.dict(os.environ, {"GPU_LOCK": str(lock),
                        "ALEXANDRIA_GPU_LOCK_HELD": "", "ALEXANDRIA_GPU_LOCK_PID": ""}), \
                     patch.object(benchmark, "APP_DIR", tmp), \
                     patch.object(sys, "argv", ["tts_vram_benchmark.py", "--sizes", "1"]), \
                     patch.object(benchmark, "load_app_config", return_value={}), \
                     patch("tts.TTSEngine", side_effect=AssertionError("engine loaded before lease refusal")) as engine, \
                     patch.object(benchmark, "vram_state", return_value=None), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "GPU lock is held"):
                        benchmark.main()
                    engine.assert_not_called()
                self.assertFalse((Path(tmp) / "benchmark_results.json").exists())

    def test_lease_covers_model_sweep_and_publication_and_releases_afterward(self):
        self.run_scenario(None)

    def test_all_setup_and_publication_failures_release_lease(self):
        for phase in ("config", "engine", "load", "warmup", "sweep", "save"):
            with self.subTest(phase=phase):
                self.run_scenario(phase)

    def run_scenario(self, failure):
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            lock = root / "gpu.lock"
            phases = []
            def check_held(phase):
                with open(lock, "a") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                phases.append(phase)
                if phase == failure:
                    raise RuntimeError("injected " + phase)
            def config(_path):
                check_held("config")
                return {"tts": {"device": "cpu"}}
            engine = SimpleNamespace(_local_custom_model="CPU fixture",
                _init_local_custom=lambda: check_held("load"),
                ensure_custom_warmup=lambda _model: check_held("warmup"))
            def create(_config):
                check_held("engine")
                return engine
            def sweep(*_args):
                check_held("sweep")
                return []
            original_save = benchmark.save_benchmark_results
            def save(result, path):
                check_held("save")
                original_save(result, path)
            with patch.dict(os.environ, {"GPU_LOCK": str(lock),
                    "ALEXANDRIA_GPU_LOCK_HELD": "", "ALEXANDRIA_GPU_LOCK_PID": ""}), \
                 patch.object(benchmark, "APP_DIR", str(app)), \
                 patch.object(sys, "argv", ["tts_vram_benchmark.py", "--sizes", "1", "--chunks", "1"]), \
                 patch.object(benchmark, "load_app_config", side_effect=config), \
                 patch("tts.TTSEngine", side_effect=create), \
                 patch.object(benchmark, "vram_state", return_value=None), \
                 patch.object(benchmark, "gpu_name", return_value="CPU fixture"), \
                 patch.object(benchmark, "run_sweep", side_effect=sweep), \
                 patch.object(benchmark, "save_benchmark_results", side_effect=save), \
                 contextlib.redirect_stdout(io.StringIO()):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "injected " + failure):
                        benchmark.main()
                else:
                    benchmark.main()
            with open(lock, "a") as contender:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(contender, fcntl.LOCK_UN)
            if failure:
                self.assertFalse((app / "benchmark_results.json").exists())
            else:
                self.assertEqual(["config", "engine", "load", "warmup", "sweep", "save"], phases)
                result = json.loads((app / "benchmark_results.json").read_text())
                self.assertEqual("CPU fixture", result["gpu"])
                self.assertEqual([], result["baseline"])
