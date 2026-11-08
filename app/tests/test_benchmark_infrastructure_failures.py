"""Issue 1789: worker infrastructure exception is not a completed benchmark.

Fail-first regression coverage. Synthetic data only, no GPU/live services.
Infrastructure payload shape is the one emit_benchmark_worker_result()
produces on exception at worker exit 0: {"status":"failed","metrics":{},
"error": ...}. Valid measured quality negatives have status "failed" but
carry real measurements and no infrastructure error payload, and must still
complete normally.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import benchmark_core as core
import benchmark_runner as runner
import benchmark_worker_protocol as protocol

INFRA = {"status": "failed", "metrics": {}, "error": "synthetic infra boom"}
INFRA_EMPTY_ERROR = {"status": "failed", "metrics": {}, "error": ""}


def infra_result(marker, payload=INFRA):
    return SimpleNamespace(returncode=0, stdout=marker + json.dumps(payload) + "\n", stderr="")


def environment(target="local"):
    return core.build_environment_fingerprint(target, {
        "hostname": "fixture", "gpu_name": "none", "backend": "cpu",
        "python_version": "fixture", "git_commit": "fixture"})


def manifest(stage="voicelab_preparer", fixtures=None, repetitions=1):
    return {"schema_version": 1, "stage": stage, "targets": ["local"],
            "repetitions": repetitions,
            "fixtures": fixtures or [{"id": "first", "sha256": "a"}]}


def fresh_state(fixtures):
    return {"cancel": False, "status": "running", "logs": [],
            "tasks": [{"fixture_id": f["id"], "status": "pending"} for f in fixtures]}


class MetricsOnlyAdapterInfrastructureTests(unittest.TestCase):
    """All three metrics-only adapters must raise on an exit-0 infra marker."""

    def call_adapter(self, name):
        fixture = {}
        if name in ("preparer", "dedup"):
            settings = {"local_python": sys.executable}
        else:
            settings = {}
        with tempfile.TemporaryDirectory() as tmp:
            if name == "training":
                return runner._run_lora_training_worker(fixture, "local", settings, tmp, None)
            if name == "preparer":
                return runner._run_preparer_worker(fixture, "local", settings, tmp, None)
            return runner._run_dedup_worker(fixture, "local", settings, tmp, None)

    def test_metrics_only_adapters_raise_on_infra_marker(self):
        markers = {"training": "LORA_TRAINING_BENCHMARK_RESULT=",
                   "preparer": "PREPARER_BENCHMARK_RESULT=",
                   "dedup": "DEDUP_BENCHMARK_RESULT="}
        for name, marker in markers.items():
            for payload in (INFRA, INFRA_EMPTY_ERROR):
                with self.subTest(adapter=name, payload=payload), \
                     patch.object(runner, "run_benchmark_subprocess",
                                  return_value=infra_result(marker, payload)):
                    with self.assertRaises(RuntimeError):
                        self.call_adapter(name)

    def test_metrics_only_adapters_still_return_measured_success(self):
        good = {"status": "passed", "metrics": {"elapsed_seconds": 1.5}, "error": None}
        for name, marker in {"training": "LORA_TRAINING_BENCHMARK_RESULT=",
                             "preparer": "PREPARER_BENCHMARK_RESULT=",
                             "dedup": "DEDUP_BENCHMARK_RESULT="}.items():
            with self.subTest(adapter=name), \
                 patch.object(runner, "run_benchmark_subprocess",
                              return_value=infra_result(marker, good)):
                self.assertEqual(good, self.call_adapter(name))

    def test_actual_malformed_payload_subprocess_exit0_is_not_a_result(self):
        """Real native worker, real subprocess, exit 0, infra marker."""
        app_dir = Path(__file__).resolve().parent.parent
        cases = [("preparer_benchmark.py", "PREPARER_BENCHMARK_RESULT=",
                  "preparer", {"local_python": sys.executable}),
                 ("dedup_benchmark.py", "DEDUP_BENCHMARK_RESULT=",
                  "dedup", {"local_python": sys.executable}),
                 ("lora_training_benchmark.py", "LORA_TRAINING_BENCHMARK_RESULT=",
                  "training", {})]
        for filename, marker, name, settings in cases:
            with self.subTest(worker=filename), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    [sys.executable, str(app_dir / filename), "--payload", "!!!!"],
                    capture_output=True, text=True, timeout=30,
                    env=dict(os.environ, ALEXANDRIA_DATA_DIR=tmp))
                self.assertEqual(0, result.returncode, result.stderr)
                lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
                self.assertEqual(1, len(lines), result.stdout)
                payload = json.loads(lines[0][len(marker):])
                self.assertEqual("failed", payload["status"])
                self.assertEqual({}, payload["metrics"])
                self.assertIn("error", payload)
                completed = SimpleNamespace(returncode=0, stdout=result.stdout, stderr=result.stderr)
                with patch.object(runner, "run_benchmark_subprocess", return_value=completed), \
                     tempfile.TemporaryDirectory() as root:
                    call = {"preparer": lambda: runner._run_preparer_worker({}, "local", settings, root, None),
                            "dedup": lambda: runner._run_dedup_worker({}, "local", settings, root, None),
                            "training": lambda: runner._run_lora_training_worker({}, "local", settings, root, None)}[name]
                    with self.assertRaises(RuntimeError):
                        call()


class DictionaryAdapterInfrastructureTests(unittest.TestCase):
    """Dict-returning adapters: infra payload raises, measured negative completes."""

    def measured_negative(self, name):
        return {
            "export": {"status": "failed", "elapsed_seconds": 1.0, "artifact_bytes": 10,
                       "artifact_sha256": "x" * 64, "members": ["a"]},
            "profiling": {"status": "failed", "elapsed_seconds": 1.0, "voice_features": {}},
            "naming": {"status": "failed", "elapsed_seconds": 0.5, "named_ids": [],
                       "adapter_dirs": [], "manifest_sha256": "x" * 64, "backup_created": False},
            "dataset_builder": {"status": "failed", "elapsed_seconds": 1.0,
                                "audio_duration_seconds": 0.0, "outputs": [], "logs": [], "completed": 0},
        }[name]

    def call_adapter(self, name, payload):
        marker = {"export": "EXPORT_BENCHMARK_RESULT=", "profiling": "PROFILING_BENCHMARK_RESULT=",
                  "naming": "NAMING_BENCHMARK_RESULT=",
                  "dataset_builder": "DATASET_BUILDER_BENCHMARK_RESULT="}[name]
        fixture = {"zip_path": "f.zip", "model_path": "f.gguf"}
        settings = {"local_python": sys.executable}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "run_benchmark_subprocess",
                          return_value=infra_result(marker, payload)), \
             patch.object(runner, "_validate_export_fixture"):
            if name == "export":
                return runner._run_export_worker("audacity_export", fixture, "local", settings, tmp, None)
            if name == "profiling":
                return runner._run_profiling_worker(fixture, "local", settings, tmp, None)
            if name == "naming":
                named = {"entries": []}
                fixture = {**named, "sha256": runner._hash_entries(named)}
                return runner._run_naming_worker(fixture, "local", settings, tmp, None)
            content = {"description": "d", "samples": [], "global_seed": 1, "seeds": []}
            fixture = {**content, "sha256": runner._hash_entries(content)}
            return runner._run_dataset_builder_worker(fixture, "local", settings, tmp, None, {})

    def test_dictionary_adapters_raise_on_infra_marker(self):
        for name in ("export", "profiling", "naming", "dataset_builder"):
            for payload in (INFRA, INFRA_EMPTY_ERROR):
                with self.subTest(adapter=name, payload=payload):
                    with self.assertRaises(RuntimeError):
                        self.call_adapter(name, payload)

    def test_dictionary_adapters_return_measured_quality_negatives(self):
        for name in ("export", "profiling", "naming", "dataset_builder"):
            with self.subTest(adapter=name):
                negative = self.measured_negative(name)
                self.assertEqual(negative, self.call_adapter(name, negative))


class TTSPerCaseInfrastructureTests(unittest.TestCase):
    def run_tts(self, tmp, worker_cases):
        fixtures = [{"id": "voice", "sha256": "a"}]
        man = manifest("tts_generation", fixtures, repetitions=1)
        path = str(Path(tmp) / "report.json")
        state = fresh_state(fixtures)
        with patch.object(runner, "load_app_config", return_value={}), \
             patch.object(runner, "_validate_tts_fixture"), \
             patch.object(runner, "_run_tts_worker", return_value=worker_cases):
            return runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp), path, state

    def test_tts_per_case_exception_with_quality_is_not_a_measured_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = {"fixture_id": "voice", "repetition": 1, "status": "failed",
                    "metrics": {}, "error": "synthetic tts boom"}
            fixtures = [{"id": "voice", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=1)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_tts_fixture"), \
                 patch.object(runner, "_run_tts_worker", return_value=[case]):
                with self.assertRaisesRegex(RuntimeError, "synthetic tts boom"):
                    runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp)
            saved = json.loads(Path(path).read_text()) if Path(path).exists() else {"cases": []}
            self.assertEqual([], saved["cases"])
            self.assertEqual("failed", state["status"])
            self.assertEqual("failed", state["tasks"][0]["status"])

    def test_tts_measured_threshold_negative_still_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = {"fixture_id": "voice", "repetition": 1, "status": "passed",
                    "metrics": {"duration_seconds": 0.01, "silence_ratio": 0.99, "clipping_ratio": 0.5},
                    "error": None}
            report, path, state = self.run_tts(tmp, [case])
            self.assertEqual("complete", state["status"])
            self.assertEqual("done", state["tasks"][0]["status"])
            self.assertEqual("failed", report["cases"][0]["status"])
            self.assertFalse(report["cases"][0]["quality"]["passed"])


class LifecycleInfrastructureTests(unittest.TestCase):
    def run_stage(self, tmp, stage, run_name, worker_name, first_result, second_result):
        fixtures = [{"id": "first", "sha256": "a"}, {"id": "second", "sha256": "b"}]
        man = manifest(stage, fixtures)
        original = copy.deepcopy(man)
        path = str(Path(tmp) / "report.json")
        state = fresh_state(fixtures)
        with patch.object(runner, "load_app_config", return_value={}), \
             patch.object(runner, "_validate_preparer_fixture"), \
             patch.object(runner, "_validate_dedup_fixture"), \
             patch.object(runner, "_validate_lora_training_fixture"), \
             patch.object(runner, "_validate_profiling_fixture"), \
             patch.object(runner, "_validate_export_fixture"), \
             patch.object(runner, worker_name, side_effect=[first_result, second_result]) as worker:
            with self.assertRaises(RuntimeError):
                getattr(runner, run_name)(man, environment(), path, state, "unused", tmp)
            saved = json.loads(Path(path).read_text())
            self.assertEqual([{"fixture_id": "first", "repetition": 1, **first_result}], saved["cases"])
            self.assertEqual("failed", state["status"])
            self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
            self.assertEqual(original, man)
            # Resume with a measured second case: only the missing fixture re-runs.
            worker.reset_mock()
            measured = {"status": "passed", "metrics": {"elapsed_seconds": 0.2}}
            worker.side_effect = None
            worker.return_value = measured
            resumed = getattr(runner, run_name)(man, environment(), path, state, "unused", tmp)
            worker.assert_called_once()
            self.assertEqual(saved["cases"], resumed["cases"][:1])
            self.assertEqual({"fixture_id": "second", "repetition": 1, **measured}, resumed["cases"][1])
            self.assertEqual("complete", state["status"])
            self.assertTrue(all(t["status"] == "done" for t in state["tasks"]))

    def test_repetitions_infra_after_prefix_preserves_prefix_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_stage(tmp, "voicelab_preparer", "run_preparer_benchmark",
                           "_run_preparer_worker",
                           {"status": "passed", "metrics": {"elapsed_seconds": 0.1}}, INFRA)

    def test_batch_infra_after_prefix_preserves_prefix_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=2)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            first = {"fixture_id": "first", "repetition": 1, "status": "passed",
                     "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0}}
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_tts_fixture"), \
                 patch.object(runner, "_run_tts_worker",
                              side_effect=lambda *a: [first, INFRA]):
                # INFRA here stands in for a top-level worker exception payload
                # reaching the batch lifecyle as a case; it must not complete.
                with self.assertRaises(RuntimeError):
                    runner.run_benchmark_batch(
                        man, environment(), path, state,
                        lambda pending: [first, {**INFRA, "fixture_id": "first", "repetition": 2,
                                                 "quality": {"passed": False}}])
                saved = json.loads(Path(path).read_text())
                self.assertEqual([first], saved["cases"])
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])

    def test_previously_saved_infra_case_does_not_complete_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            report = core.build_benchmark_report(man, environment())
            old_case = {"fixture_id": "first", "repetition": 1, **INFRA}
            report["cases"] = [old_case]
            Path(path).write_text(json.dumps(report))
            raw_before = Path(path).read_bytes()
            measured = {"status": "passed", "metrics": {"elapsed_seconds": 0.3}}
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", return_value=measured) as worker:
                resumed = runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
            # Worker must re-run the infra-only (fixture, repetition); its
            # identifier alone cannot satisfy completion. Old evidence stays.
            worker.assert_called_once()
            self.assertEqual(old_case, resumed["cases"][0])
            self.assertEqual({"fixture_id": "first", "repetition": 1, **measured}, resumed["cases"][1])
            self.assertEqual("complete", state["status"])
            self.assertNotEqual(raw_before, Path(path).read_bytes())

    def test_measured_quality_negative_still_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_naming", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            negative = {"status": "failed", "elapsed_seconds": 0.5, "named_ids": [],
                        "adapter_dirs": [], "manifest_sha256": "x" * 64, "backup_created": False}
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_run_naming_worker", return_value=negative):
                report = runner.run_naming_benchmark(man, environment(), path, state, "unused", tmp)
            self.assertEqual("complete", state["status"])
            self.assertEqual("done", state["tasks"][0]["status"])
            self.assertEqual(negative["status"], report["cases"][0]["status"])

    def test_cancellation_remains_authoritative_over_infra_and_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            state["cancel"] = True
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", side_effect=RuntimeError("must not run")) as worker:
                report = runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
            worker.assert_not_called()
            self.assertEqual("cancelled", state["status"])
            self.assertEqual([], report["cases"])

    def test_latest_attempt_semantics_both_directions(self):
        measured = {"status": "passed", "metrics": {"elapsed_seconds": 0.3}}
        for cases, expect_call in (
                ([{"fixture_id": "first", "repetition": 1, **INFRA},
                  {"fixture_id": "first", "repetition": 1, **measured}], False),
                ([{"fixture_id": "first", "repetition": 1, **measured},
                  {"fixture_id": "first", "repetition": 1, **INFRA}], True)):
            with self.subTest(expect_call=expect_call), tempfile.TemporaryDirectory() as tmp:
                fixtures = [{"id": "first", "sha256": "a"}]
                man = manifest("voicelab_preparer", fixtures)
                path = str(Path(tmp) / "report.json")
                state = fresh_state(fixtures)
                report = core.build_benchmark_report(man, environment())
                report["cases"] = copy.deepcopy(cases)
                Path(path).write_text(json.dumps(report))
                with patch.object(runner, "load_app_config", return_value={}), \
                     patch.object(runner, "_validate_preparer_fixture"), \
                     patch.object(runner, "_run_preparer_worker", return_value=measured) as worker:
                    resumed = runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
                self.assertEqual(expect_call, worker.called)
                self.assertEqual(cases, resumed["cases"][:len(cases)])
                if expect_call:
                    self.assertEqual({"fixture_id": "first", "repetition": 1, **measured}, resumed["cases"][-1])
                self.assertEqual("complete", state["status"])

    def test_cancellation_queued_by_execute_before_infra_return_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)

            def worker(*args):
                state["cancel"] = True
                return INFRA

            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", side_effect=worker):
                report = runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
            self.assertEqual("cancelled", state["status"])
            self.assertEqual([], report["cases"])
            self.assertEqual("cancelled", state["tasks"][0]["status"])

    def test_cancellation_queued_by_batch_before_infra_yield_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=2)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            first = {"fixture_id": "first", "repetition": 1, "status": "passed",
                     "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0}}

            def execute(pending):
                yield first
                state["cancel"] = True
                yield {**INFRA, "fixture_id": "first", "repetition": 2, "quality": {"passed": False}}

            report = runner.run_benchmark_batch(man, environment(), path, state, execute)
            self.assertEqual("cancelled", state["status"])
            self.assertEqual([first], report["cases"])

    def test_stale_manifest_and_environment_resume_refused_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            report = core.build_benchmark_report(man, environment())
            report["cases"] = [{"fixture_id": "first", "repetition": 1,
                                "status": "passed", "metrics": {"elapsed_seconds": 0.1}}]
            Path(path).write_text(json.dumps(report))
            raw_before = Path(path).read_bytes()
            stale_man = manifest("voicelab_preparer", [{"id": "first", "sha256": "z"}])
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                core.load_resumable_benchmark_report(path, stale_man, environment())
            stale_env = core.build_environment_fingerprint("local", {
                "hostname": "other", "gpu_name": "none", "backend": "cpu",
                "python_version": "fixture", "git_commit": "fixture"})
            with self.assertRaisesRegex(ValueError, "environment changed"):
                core.load_resumable_benchmark_report(path, man, stale_env)
            self.assertEqual(raw_before, Path(path).read_bytes())


class CancellationPropagationTests(unittest.TestCase):
    def test_cancel_after_subprocess_before_parse_history_is_cancelled(self):
        """Real adapter/parser -> core -> isolated history, cancel queued in subprocess."""
        import core as core_module
        from run_history import list_runs
        import subprocess as sp
        key = "_test_1789_cancel_parse_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest("voicelab_preparer", fixtures)
        man["settings"] = {"local_python": sys.executable}
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]

                def completed_worker(*args, **kwargs):
                    state["cancel"] = True
                    return sp.CompletedProcess([], 0, "PREPARER_BENCHMARK_RESULT=" + json.dumps(INFRA) + "\n", "")

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, "_validate_preparer_fixture"), \
                         patch.object(runner, "run_benchmark_subprocess", side_effect=completed_worker):
                        runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)

                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                self.assertEqual(1, len(records))
                self.assertEqual("cancelled", state["status"])
                self.assertEqual("cancelled", records[0]["status"])
                self.assertIsNone(records[0].get("error"))
        finally:
            core_module.process_state.pop(key, None)

    def test_batch_exception_with_cancel_queued_returns_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=2)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            first = {"fixture_id": "first", "repetition": 1, "status": "passed",
                     "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0}}

            def execute(pending):
                yield first
                state["cancel"] = True
                raise RuntimeError("synthetic infra boom")

            report = runner.run_benchmark_batch(man, environment(), path, state, execute)
            self.assertEqual("cancelled", state["status"])
            self.assertEqual([first], report["cases"])

    def test_batch_exception_without_cancel_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=1)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)

            def execute(pending):
                raise RuntimeError("synthetic infra boom")
                yield  # pragma: no cover

            with self.assertRaisesRegex(RuntimeError, "synthetic infra boom"):
                runner.run_benchmark_batch(man, environment(), path, state, execute)
            self.assertEqual("failed", state["status"])
            self.assertEqual("failed", state["tasks"][0]["status"])

    def test_repetitions_keyboard_interrupt_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)

            def worker(*args):
                raise KeyboardInterrupt()

            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", side_effect=worker):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)

    def test_repetitions_system_exit_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", side_effect=SystemExit(3)):
                with self.assertRaises(SystemExit):
                    runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)


class HistoryInfrastructureTests(unittest.TestCase):
    def run_history(self, stage, run_name, marker, payload, validate_name):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_benchmark_infra_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest(stage, fixtures)
        man["settings"] = {"local_python": sys.executable}
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, validate_name), \
                         patch.object(runner, "run_benchmark_subprocess",
                                      return_value=infra_result(marker, payload)):
                        getattr(runner, run_name)(man, environment(), path, state, "unused", tmp)

                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                return records, state
        finally:
            core_module.process_state.pop(key, None)

    def test_infra_history_traverses_real_adapter_and_parser(self):
        records, state = self.run_history(
            "voicelab_preparer", "run_preparer_benchmark",
            "PREPARER_BENCHMARK_RESULT=", INFRA, "_validate_preparer_fixture")
        self.assertEqual(1, len(records))
        self.assertEqual("failed", records[0]["status"])
        self.assertIn("synthetic infra boom", records[0]["error"])
        self.assertEqual("failed", state["status"])
        self.assertEqual("failed", state["tasks"][0]["status"])
        self.assertFalse(state["running"])

    def test_measured_negative_history_completed_control(self):
        negative = {"status": "failed", "elapsed_seconds": 0.5, "named_ids": [],
                    "adapter_dirs": [], "manifest_sha256": "x" * 64, "backup_created": False}
        # Naming fixture hash is patched at validate level; the worker itself
        # only checks the fixture hash, so patch it via the runner hash path.
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_benchmark_negative_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest("voicelab_naming", fixtures)
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                named = {"entries": []}
                man["fixtures"] = [{**named, "id": "first", "sha256": runner._hash_entries(named)}]

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, "run_benchmark_subprocess",
                                      return_value=infra_result("NAMING_BENCHMARK_RESULT=", negative)):
                        runner.run_naming_benchmark(man, environment(), path, state, "unused", tmp)

                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                self.assertEqual(1, len(records))
                self.assertEqual("completed", records[0]["status"])
                self.assertEqual("complete", state["status"])
                self.assertEqual("done", state["tasks"][0]["status"])
        finally:
            core_module.process_state.pop(key, None)


if __name__ == "__main__":
    unittest.main()
