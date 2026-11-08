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
            self.assertTrue(Path(path).exists())
            saved = json.loads(Path(path).read_text())
            self.assertEqual([{**case, "quality": {"passed": False}}], saved["cases"])
            self.assertEqual("synthetic tts boom", saved["cases"][0]["error"])
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
            self.assertEqual([{"fixture_id": "first", "repetition": 1, **first_result},
                              {"fixture_id": "second", "repetition": 1, **second_result}], saved["cases"])
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
            self.assertEqual(saved["cases"], resumed["cases"][:2])
            self.assertEqual({"fixture_id": "second", "repetition": 1, **measured}, resumed["cases"][2])
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
                self.assertEqual([first, {**INFRA, "fixture_id": "first", "repetition": 2,
                                          "quality": {"passed": False}}], saved["cases"])
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
            self.assertEqual([{"fixture_id": "first", "repetition": 1, **INFRA}], report["cases"])
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
            self.assertEqual([first, {**INFRA, "fixture_id": "first", "repetition": 2,
                                      "quality": {"passed": False}}], report["cases"])

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


class BatchRetentionTests(unittest.TestCase):
    def test_tts_stage_infra_then_measured_retains_both_and_fails_history(self):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_batch_retention_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest("tts_generation", fixtures, repetitions=2)
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                cases = [{"fixture_id": "first", "repetition": 1, **INFRA},
                         {"fixture_id": "first", "repetition": 2, "status": "passed",
                          "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0}, "error": None}]

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, "_validate_tts_fixture"), \
                         patch.object(runner, "_run_tts_worker", return_value=cases):
                        runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp)

                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])
                self.assertEqual(1, len(records))
                self.assertEqual("failed", records[0]["status"])
                self.assertIn("synthetic infra boom", records[0]["error"])
                saved = json.loads(Path(path).read_text())
                self.assertEqual(2, len(saved["cases"]))
                self.assertEqual("synthetic infra boom", saved["cases"][0]["error"])
                self.assertEqual("passed", saved["cases"][1]["status"])
        finally:
            core_module.process_state.pop(key, None)

    def test_batch_resume_calls_worker_only_for_infra_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=2)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            report = core.build_benchmark_report(man, environment())
            infra_case = {"fixture_id": "first", "repetition": 1, **INFRA, "quality": {"passed": False}}
            measured2 = {"fixture_id": "first", "repetition": 2, "status": "passed",
                         "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0},
                         "error": None, "quality": {"passed": True}}
            report["cases"] = [infra_case, measured2]
            Path(path).write_text(json.dumps(report))
            measured1 = {"fixture_id": "first", "repetition": 1, "status": "passed",
                         "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0},
                         "error": None, "quality": {"passed": True}}
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_tts_fixture"), \
                 patch.object(runner, "_run_tts_worker", return_value=[measured1]) as worker:
                resumed = runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp)
            worker.assert_called_once()
            payload = worker.call_args.args[0]
            self.assertEqual([1], payload["fixtures"][0]["repetition_numbers"])
            self.assertEqual([infra_case, measured2, measured1], resumed["cases"])
            self.assertEqual("complete", state["status"])

    def test_all_infra_batch_yields_explicit_failed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("tts_generation", fixtures, repetitions=2)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            cases = [{"fixture_id": "first", "repetition": 1, **INFRA, "quality": {"passed": False}},
                     {"fixture_id": "first", "repetition": 2, **INFRA, "quality": {"passed": False}}]
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_tts_fixture"), \
                 patch.object(runner, "_run_tts_worker", return_value=cases):
                with self.assertRaisesRegex(RuntimeError, "synthetic infra boom"):
                    runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp)
            self.assertTrue(Path(path).exists())
            saved = json.loads(Path(path).read_text())
            self.assertEqual(cases, saved["cases"])
            self.assertEqual("failed", state["status"])
            self.assertEqual("failed", state["tasks"][0]["status"])

    def test_repetitions_infra_retains_case_and_starts_no_more_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}, {"id": "second", "sha256": "b"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", return_value=INFRA) as worker:
                with self.assertRaisesRegex(RuntimeError, "synthetic infra boom"):
                    runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
            worker.assert_called_once()
            saved = json.loads(Path(path).read_text())
            self.assertEqual([{"fixture_id": "first", "repetition": 1, **INFRA}], saved["cases"])
            self.assertEqual("failed", state["status"])

    def test_bounded_history_error_with_full_raw_error_in_report(self):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_bounded_history"
        long_error = "x" * 5000 + "tail"
        infra_long = {"status": "failed", "metrics": {}, "error": long_error}
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest("tts_generation", fixtures, repetitions=1)
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                cases = [{"fixture_id": "first", "repetition": 1, **infra_long}]

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, "_validate_tts_fixture"), \
                         patch.object(runner, "_run_tts_worker", return_value=cases):
                        runner.run_tts_generation_benchmark(man, environment(), path, state, "unused", tmp)

                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                self.assertEqual("failed", records[0]["status"])
                self.assertLessEqual(len(records[0]["error"]), 4000)
                self.assertTrue(records[0]["error"].endswith("tail"))
                saved = json.loads(Path(path).read_text())
                self.assertEqual(long_error, saved["cases"][0]["error"])
        finally:
            core_module.process_state.pop(key, None)


class PublicationGuardTests(unittest.TestCase):
    def run_repetitions_through_core(self, save_error):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_publication_history"
        fixtures = [{"id": "first", "sha256": "a"}, {"id": "second", "sha256": "b"}]
        man = manifest("voicelab_preparer", fixtures)
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": f["id"], "status": "pending"} for f in fixtures]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                measured = {"status": "passed", "metrics": {"elapsed_seconds": 0.1}}
                original_save = runner.save_benchmark_report
                saves = []

                def flaky_save(report_path, report):
                    saves.append(report)
                    if len(saves) == 2:
                        raise save_error
                    return original_save(report_path, report)

                def callback():
                    with patch.object(runner, "load_app_config", return_value={}), \
                         patch.object(runner, "_validate_preparer_fixture"), \
                         patch.object(runner, "_run_preparer_worker", return_value=measured), \
                         patch.object(runner, "save_benchmark_report", side_effect=flaky_save):
                        runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)

                propagated = None
                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    try:
                        core_module._run_claimed_background_task(key, callback)
                    except BaseException as exc:  # SystemExit escapes core; OSError is caught by core
                        propagated = exc
                saved_cases = json.loads(Path(path).read_text())["cases"] if Path(path).exists() else []
                return list_runs(history_dir), state, saved_cases, propagated
        finally:
            core_module.process_state.pop(key, None)

    def test_publication_oserror_fails_state_and_history_and_keeps_prefix(self):
        records, state, saved, propagated = self.run_repetitions_through_core(OSError("synthetic publication disk full"))
        self.assertIsNone(propagated)  # core catches Exception, records it in history
        self.assertEqual(1, len(records))
        self.assertEqual("failed", state["status"])
        self.assertEqual("failed", records[0]["status"])
        self.assertIn("synthetic publication disk full", records[0]["error"])
        self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
        self.assertEqual([{"fixture_id": "first", "repetition": 1, "status": "passed",
                           "metrics": {"elapsed_seconds": 0.1}}], saved)

    def test_publication_system_exit_propagates_and_fails_history_and_keeps_prefix(self):
        records, state, saved, propagated = self.run_repetitions_through_core(SystemExit("synthetic publication shutdown"))
        self.assertIsInstance(propagated, SystemExit)
        self.assertEqual(1, len(records))
        self.assertEqual("failed", state["status"])
        self.assertEqual("failed", records[0]["status"])
        self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
        self.assertEqual([{"fixture_id": "first", "repetition": 1, "status": "passed",
                           "metrics": {"elapsed_seconds": 0.1}}], saved)

    def test_batch_publication_system_exit_propagates_and_fails_history(self):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_batch_publication_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest("tts_generation", fixtures, repetitions=2)
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "running"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                case = {"fixture_id": "first", "repetition": 1, "status": "passed",
                        "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0}, "error": None}
                original_save = runner.save_benchmark_report
                saves = []

                def flaky_save(report_path, report):
                    saves.append(report)
                    if len(saves) == 2:
                        raise SystemExit("synthetic batch publication shutdown")
                    return original_save(report_path, report)

                def callback():
                    runner.run_benchmark_batch(man, environment(), path, state, lambda pending: [case, case])

                propagated = None
                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir), \
                     patch.object(runner, "save_benchmark_report", side_effect=flaky_save):
                    try:
                        core_module._run_claimed_background_task(key, callback)
                    except BaseException as exc:
                        propagated = exc
                self.assertIsInstance(propagated, SystemExit)
                records = list_runs(history_dir)
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", records[0]["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])
                saved = json.loads(Path(path).read_text())
                self.assertEqual([case], saved["cases"])
        finally:
            core_module.process_state.pop(key, None)


class BoundaryStatusTests(unittest.TestCase):
    def test_direct_report_load_fatalities_fail_boundary_through_core(self):
        import core as core_module
        from run_history import list_runs
        for lifecycle in ("repetitions", "batch"):
            for fatal in (SystemExit("synthetic load shutdown"), KeyboardInterrupt(), OSError("synthetic load disk full")):
                with self.subTest(lifecycle=lifecycle, fatal=type(fatal).__name__):
                    key = "_test_1789_load_boundary"
                    fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                    man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
                           "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
                    core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "complete",
                                                      "tasks": [{"fixture_id": "done", "status": "done"},
                                                                {"fixture_id": "pending", "status": "pending"}]}
                    try:
                        with tempfile.TemporaryDirectory() as tmp:
                            history_dir = str(Path(tmp) / "history")
                            path = str(Path(tmp) / "report.json")
                            state = core_module.process_state[key]
                            env = environment()
                            report = core.build_benchmark_report(man, env)
                            prefix = {"fixture_id": "done", "repetition": 1, "status": "passed",
                                      "metrics": {"elapsed_seconds": 0.1}}
                            report["cases"] = [prefix]
                            Path(path).write_text(json.dumps(report))

                            def callback():
                                with patch.object(runner, "load_resumable_benchmark_report", side_effect=fatal):
                                    if lifecycle == "batch":
                                        runner.run_benchmark_batch(man, env, path, state, lambda pending: [], report=None)
                                    else:
                                        runner.run_benchmark_repetitions(man, env, path, state, lambda f, r: {}, report=None)

                            propagated = None
                            with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                                try:
                                    core_module._run_claimed_background_task(key, callback)
                                except BaseException as exc:
                                    propagated = exc
                            records = list_runs(history_dir)
                            self.assertEqual("failed", state["status"])
                            self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
                            self.assertEqual(1, len(records))
                            self.assertEqual("failed", records[0]["status"])
                            self.assertEqual([prefix], json.loads(Path(path).read_text())["cases"])
                            if isinstance(fatal, OSError):
                                self.assertIsNone(propagated)
                                self.assertIn("synthetic load disk full", records[0]["error"])
                            else:
                                self.assertIs(fatal, propagated)
                    finally:
                        core_module.process_state.pop(key, None)

    def test_direct_load_failure_with_empty_manifest_fails_all_unfinished_tasks(self):
        import core as core_module
        from run_history import list_runs
        for lifecycle in ("repetitions", "batch"):
            with self.subTest(lifecycle=lifecycle):
                key = "_test_1789_load_boundary_empty_manifest"
                fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                good_man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
                            "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
                core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "complete",
                                                  "tasks": [{"fixture_id": "done", "status": "done"},
                                                            {"fixture_id": "pending", "status": "pending"},
                                                            {"status": "pending"}]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        history_dir = str(Path(tmp) / "history")
                        path = str(Path(tmp) / "report.json")
                        state = core_module.process_state[key]
                        env = environment()
                        report = core.build_benchmark_report(good_man, env)
                        prefix = {"fixture_id": "done", "repetition": 1, "status": "passed",
                                  "metrics": {"elapsed_seconds": 0.1}}
                        report["cases"] = [prefix]
                        Path(path).write_text(json.dumps(report))
                        fatal = SystemExit("synthetic empty-manifest load shutdown")

                        def callback():
                            with patch.object(runner, "load_resumable_benchmark_report", side_effect=fatal):
                                if lifecycle == "batch":
                                    runner.run_benchmark_batch({}, env, path, state, lambda pending: [])
                                else:
                                    runner.run_benchmark_repetitions({}, env, path, state, lambda f, r: {})

                        propagated = None
                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                            try:
                                core_module._run_claimed_background_task(key, callback)
                            except BaseException as exc:
                                propagated = exc
                        self.assertIs(fatal, propagated)
                        self.assertEqual("failed", state["status"])
                        self.assertEqual(["done", "failed", "failed"], [t["status"] for t in state["tasks"]])
                        self.assertEqual("failed", list_runs(history_dir)[0]["status"])
                        self.assertEqual([prefix], json.loads(Path(path).read_text())["cases"])
                finally:
                    core_module.process_state.pop(key, None)


class WrapperBoundaryTests(unittest.TestCase):
    def test_wrapper_fatal_and_false_flag_boundaries_through_core(self):
        import core as core_module
        from run_history import list_runs
        from benchmark_execution import BenchmarkCancelled
        for mode in ("preamble_system_exit", "load_system_exit", "false_flag_cancel", "load_benchmark_cancelled"):
            with self.subTest(mode=mode):
                key = "_test_1789_wrapper_boundary"
                fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                man = {"schema_version": 1, "stage": "voicelab_preparer", "targets": ["local"],
                       "repetitions": 1, "fixtures": fixtures, "settings": {"local_python": sys.executable}}
                core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                                  "tasks": [{"fixture_id": "done", "status": "done"},
                                                            {"fixture_id": "pending", "status": "pending"}]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        history_dir = str(Path(tmp) / "history")
                        path = str(Path(tmp) / "report.json")
                        state = core_module.process_state[key]
                        env = environment()
                        report = core.build_benchmark_report(man, env)
                        prefix = {"fixture_id": "done", "repetition": 1, "status": "passed",
                                  "metrics": {"elapsed_seconds": 0.1}}
                        report["cases"] = [prefix]
                        Path(path).write_text(json.dumps(report))
                        fatal = SystemExit("synthetic wrapper fatal")

                        def callback():
                            if mode == "preamble_system_exit":
                                with patch.object(runner, "_validate_preparer_fixture", side_effect=fatal), \
                                     patch.object(runner, "load_app_config", return_value={}):
                                    runner.run_preparer_benchmark(man, env, path, state, "unused", tmp)
                            elif mode in ("load_system_exit", "load_benchmark_cancelled"):
                                loads = []
                                load_fatal = BenchmarkCancelled("synthetic finalization cancel") if mode == "load_benchmark_cancelled" else fatal

                                def flaky_load(rp, manifest_arg, env_arg):
                                    loads.append(1)
                                    if len(loads) == 2:
                                        raise load_fatal
                                    return copy.deepcopy(report)

                                def worker(*args):
                                    state["cancel"] = True
                                    raise BenchmarkCancelled("cancelled")

                                with patch.object(runner, "load_app_config", return_value={}), \
                                     patch.object(runner, "_validate_preparer_fixture"), \
                                     patch.object(runner, "_run_preparer_worker", side_effect=worker), \
                                     patch.object(runner, "load_resumable_benchmark_report", side_effect=flaky_load):
                                    runner.run_preparer_benchmark(man, env, path, state, "unused", tmp)
                            else:
                                with patch.object(runner, "_validate_preparer_fixture",
                                                  side_effect=BenchmarkCancelled("cancelled")), \
                                     patch.object(runner, "load_app_config", return_value={}):
                                    runner.run_preparer_benchmark(man, env, path, state, "unused", tmp)

                        propagated = None
                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                            try:
                                core_module._run_claimed_background_task(key, callback)
                            except BaseException as exc:
                                propagated = exc
                        records = list_runs(history_dir)
                        self.assertEqual(1, len(records))
                        self.assertEqual("failed", state["status"])
                        self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
                        self.assertEqual("failed", records[0]["status"])
                        if mode == "false_flag_cancel":
                            self.assertIsNone(propagated)
                        elif mode == "load_benchmark_cancelled":
                            self.assertIsInstance(propagated, BenchmarkCancelled)
                        else:
                            self.assertIs(fatal, propagated)
                        self.assertEqual([prefix], json.loads(Path(path).read_text())["cases"])
                finally:
                    core_module.process_state.pop(key, None)


class FinalReviewTests(unittest.TestCase):
    def test_direct_resume_failed_state_with_queued_cancel_both_lifecycles(self):
        import core as core_module
        from run_history import list_runs
        from benchmark_execution import BenchmarkCancelled
        for lifecycle in ("repetitions", "batch"):
            with self.subTest(lifecycle=lifecycle):
                key = "_test_1789_direct_resume_cancel"
                fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
                       "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
                # Prior failed invocation state is reused by a direct resume.
                core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "failed",
                                                  "cancel": True,
                                                  "tasks": [{"fixture_id": "done", "status": "done"},
                                                            {"fixture_id": "pending", "status": "failed"}]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        history_dir = str(Path(tmp) / "history")
                        path = str(Path(tmp) / "report.json")
                        state = core_module.process_state[key]
                        env = environment()
                        report = core.build_benchmark_report(man, env)
                        prefix = {"fixture_id": "done", "repetition": 1, "status": "passed",
                                  "metrics": {"elapsed_seconds": 0.1}}
                        report["cases"] = [prefix]
                        Path(path).write_text(json.dumps(report))

                        def execute_case(fixture, repetition):
                            raise AssertionError("worker must not execute")

                        def execute_batch(pending):
                            raise AssertionError("worker must not execute")
                            yield  # pragma: no cover

                        def callback():
                            if lifecycle == "batch":
                                runner.run_benchmark_batch(man, env, path, state, execute_batch)
                            else:
                                runner.run_benchmark_repetitions(man, env, path, state, execute_case)

                        propagated = None
                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                            try:
                                core_module._run_claimed_background_task(key, callback)
                            except BaseException as exc:
                                propagated = exc
                        # New invocation begins running; prequeued cancel then
                        # applies at the first boundary (returns, worker never runs).
                        self.assertIsNone(propagated)
                        self.assertEqual("cancelled", state["status"])
                        self.assertEqual(1, len(list_runs(history_dir)))
                        self.assertEqual("cancelled", list_runs(history_dir)[0]["status"])
                        self.assertEqual([prefix], json.loads(Path(path).read_text())["cases"])
                finally:
                    core_module.process_state.pop(key, None)

    def test_decorated_stage_cancellation_publishes_once_through_core(self):
        import core as core_module
        from run_history import list_runs
        from benchmark_execution import BenchmarkCancelled
        for stage_name, run_name, worker_name, validate_name in (
                ("preparer", "run_preparer_benchmark", "_run_preparer_worker", "_validate_preparer_fixture"),
                ("tts", "run_tts_generation_benchmark", "_run_tts_worker", "_validate_tts_fixture")):
            with self.subTest(stage=stage_name):
                key = "_test_1789_decorated_cancel_once"
                fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                stage = "voicelab_preparer" if stage_name == "preparer" else "tts_generation"
                man = {"schema_version": 1, "stage": stage, "targets": ["local"], "repetitions": 1,
                       "fixtures": fixtures, "settings": {"local_python": sys.executable}}
                core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                                  "tasks": [{"fixture_id": "done", "status": "done"},
                                                            {"fixture_id": "pending", "status": "pending"}]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        history_dir = str(Path(tmp) / "history")
                        path = str(Path(tmp) / "report.json")
                        state = core_module.process_state[key]
                        env = environment()
                        report = core.build_benchmark_report(man, env)
                        prefix = {"fixture_id": "done", "repetition": 1, "status": "passed",
                                  "metrics": {"duration_seconds": 1, "silence_ratio": 0, "clipping_ratio": 0},
                                  "error": None, "quality": {"passed": True}}
                        report["cases"] = [prefix]
                        Path(path).write_text(json.dumps(report))
                        original_save = runner.save_benchmark_report
                        attempts = []

                        def counting_save(report_path, saved_report):
                            attempts.append(saved_report)
                            if len(attempts) >= 2:
                                raise OSError("synthetic second cancellation publication")
                            return original_save(report_path, saved_report)

                        def worker(*args):
                            state["cancel"] = True
                            raise BenchmarkCancelled("cancelled")

                        def callback():
                            with patch.object(runner, "load_app_config", return_value={}), \
                                 patch.object(runner, validate_name), \
                                 patch.object(runner, worker_name, side_effect=worker), \
                                 patch.object(runner, "save_benchmark_report", side_effect=counting_save):
                                getattr(runner, run_name)(man, env, path, state, "unused", tmp)

                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                            core_module._run_claimed_background_task(key, callback)
                        records = list_runs(history_dir)
                        self.assertEqual(1, len(attempts))
                        self.assertEqual("cancelled", state["status"])
                        self.assertEqual(1, len(records))
                        self.assertEqual("cancelled", records[0]["status"])
                        self.assertEqual([prefix], json.loads(Path(path).read_text())["cases"])
                finally:
                    core_module.process_state.pop(key, None)

    def test_publication_before_progress_log_failure_retains_case(self):
        import core as core_module
        from run_history import list_runs

        class OnceFailingLogs(list):
            def __init__(self, path):
                super().__init__()
                self.path = path
                self.attempts = 0
                self.published_before_raise = []

            def append(self, item):
                self.attempts += 1
                if self.attempts == 1:
                    self.published_before_raise = json.loads(Path(self.path).read_text())["cases"]
                    raise MemoryError("synthetic progress log failure")
                return super().append(item)

        for lifecycle in ("repetitions", "batch"):
            with self.subTest(lifecycle=lifecycle):
                key = "_test_1789_progress_publication_history"
                fixtures = [{"id": "first", "sha256": "a"}]
                man = {"schema_version": 1, "stage": "script_generation", "targets": ["local"],
                       "repetitions": 1, "fixtures": fixtures}
                with tempfile.TemporaryDirectory() as tmp:
                    history_dir = str(Path(tmp) / "history")
                    path = str(Path(tmp) / "report.json")
                    logs = OnceFailingLogs(path)
                    core_module.process_state[key] = {"running": True, "logs": logs, "process": None, "status": "running",
                                                      "tasks": [{"fixture_id": "first", "status": "running"}]}
                    try:
                        state = core_module.process_state[key]
                        env = environment()
                        measured = {"fixture_id": "first", "repetition": 1, "status": "passed",
                                    "metrics": {"elapsed_seconds": 0.1}}

                        def execute_case(fixture, repetition):
                            state["cancel"] = True
                            return {"status": "passed", "metrics": {"elapsed_seconds": 0.1}}

                        def execute_batch(pending):
                            state["cancel"] = True
                            yield measured

                        def callback():
                            if lifecycle == "batch":
                                runner.run_benchmark_batch(man, env, path, state, execute_batch)
                            else:
                                runner.run_benchmark_repetitions(man, env, path, state, execute_case)

                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                            core_module._run_claimed_background_task(key, callback)
                        records = list_runs(history_dir)
                        # Case was actually on disk before the log append failed.
                        self.assertEqual([measured], logs.published_before_raise)
                        self.assertEqual("cancelled", state["status"])
                        self.assertEqual(1, len(records))
                        self.assertEqual("cancelled", records[0]["status"])
                        self.assertEqual([measured], json.loads(Path(path).read_text())["cases"])
                    finally:
                        core_module.process_state.pop(key, None)


class CancellationReviewTests(unittest.TestCase):
    PREFIX = {"fixture_id": "done", "repetition": 1, "status": "passed", "metrics": {"elapsed_seconds": 0.1}}

    def test_transient_cancellation_publication_failure_stays_failed(self):
        import core as core_module
        from run_history import list_runs
        for lifecycle in ("repetitions", "batch"):
            with self.subTest(lifecycle=lifecycle):
                key = "_test_1789_transient_publication_history"
                fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
                man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
                       "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
                core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                                  "cancel": True,
                                                  "tasks": [{"fixture_id": "done", "status": "done"},
                                                            {"fixture_id": "pending", "status": "pending"}]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        history_dir = str(Path(tmp) / "history")
                        path = str(Path(tmp) / "report.json")
                        state = core_module.process_state[key]
                        env = environment()
                        report = core.build_benchmark_report(man, env)
                        report["cases"] = [copy.deepcopy(self.PREFIX)]
                        Path(path).write_text(json.dumps(report))
                        original_save = runner.save_benchmark_report
                        attempts = []

                        def transient_save(report_path, saved_report):
                            attempts.append(saved_report)
                            if len(attempts) == 1:
                                raise OSError("synthetic transient publication failure")
                            return original_save(report_path, saved_report)

                        def execute_case(fixture, repetition):
                            raise AssertionError("cancelled lifecycle must not start work")

                        def execute_batch(pending):
                            raise AssertionError("cancelled lifecycle must not start work")
                            yield  # pragma: no cover

                        def callback():
                            if lifecycle == "batch":
                                runner.run_benchmark_batch(man, env, path, state, execute_batch)
                            else:
                                runner.run_benchmark_repetitions(man, env, path, state, execute_case)

                        with patch.object(core_module, "RUN_HISTORY_DIR", history_dir), \
                             patch.object(runner, "save_benchmark_report", side_effect=transient_save):
                            core_module._run_claimed_background_task(key, callback)
                        records = list_runs(history_dir)
                        self.assertEqual(1, len(attempts))
                        self.assertEqual("failed", state["status"])
                        self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
                        self.assertEqual(1, len(records))
                        self.assertEqual("failed", records[0]["status"])
                        self.assertIn("synthetic transient publication failure", records[0]["error"])
                        saved = json.loads(Path(path).read_text())
                        self.assertEqual([self.PREFIX], saved["cases"])
                finally:
                    core_module.process_state.pop(key, None)

    def run_direct_cancelled_through_core(self, lifecycle, cancel_flag, create_report=True):
        import core as core_module
        from run_history import list_runs
        from benchmark_execution import BenchmarkCancelled
        key = "_test_1789_direct_cancel_history"
        fixtures = [{"id": "done", "sha256": "a"}, {"id": "pending", "sha256": "b"}]
        man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
               "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "done", "status": "done"},
                                                    {"fixture_id": "pending", "status": "pending"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                env = environment()
                if create_report:
                    report = core.build_benchmark_report(man, env)
                    report["cases"] = [copy.deepcopy(self.PREFIX)]
                    Path(path).write_text(json.dumps(report))

                def execute_case(fixture, repetition):
                    state["cancel"] = cancel_flag
                    raise BenchmarkCancelled("cancelled")

                def execute_batch(pending):
                    state["cancel"] = cancel_flag
                    raise BenchmarkCancelled("cancelled")
                    yield  # pragma: no cover

                def callback():
                    if lifecycle == "batch":
                        runner.run_benchmark_batch(man, env, path, state, execute_batch)
                    else:
                        runner.run_benchmark_repetitions(man, env, path, state, execute_case)

                propagated = None
                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    try:
                        core_module._run_claimed_background_task(key, callback)
                    except BaseException as exc:
                        propagated = exc
                saved = json.loads(Path(path).read_text()) if Path(path).exists() else {"cases": []}
                return list_runs(history_dir), state, saved, propagated
        finally:
            core_module.process_state.pop(key, None)

    def test_direct_benchmark_cancelled_flag_true_and_false_both_lifecycles(self):
        from benchmark_execution import BenchmarkCancelled
        for lifecycle in ("repetitions", "batch"):
            records, state, saved, propagated = self.run_direct_cancelled_through_core(lifecycle, True)
            with self.subTest(lifecycle=lifecycle, cancel=True):
                self.assertIsInstance(propagated, BenchmarkCancelled)
                self.assertEqual("cancelled", state["status"])
                self.assertEqual(["done", "cancelled"], [t["status"] for t in state["tasks"]])
                self.assertEqual(1, len(records))
                self.assertEqual("cancelled", records[0]["status"])
                self.assertEqual([self.PREFIX], saved["cases"])
            records, state, saved, propagated = self.run_direct_cancelled_through_core(lifecycle, False)
            with self.subTest(lifecycle=lifecycle, cancel=False):
                self.assertIsInstance(propagated, BenchmarkCancelled)
                self.assertEqual("failed", state["status"])
                self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
                self.assertEqual(1, len(records))
                self.assertEqual("failed", records[0]["status"])
                self.assertEqual([self.PREFIX], saved["cases"])

    def run_publication_failure_through_core(self, lifecycle, original_name, fatal):
        import core as core_module
        from run_history import list_runs
        from benchmark_execution import BenchmarkCancelled
        key = "_test_1789_cancel_publication_history"
        fixtures = [{"id": "first", "sha256": "a"}, {"id": "second", "sha256": "b"}]
        man = {"schema_version": 1, "stage": "script_generation" if lifecycle == "batch" else "voicelab_preparer",
               "targets": ["local"], "repetitions": 1, "fixtures": fixtures}
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": f["id"], "status": "pending"} for f in fixtures]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                env = environment()
                original_save = runner.save_benchmark_report
                saves = []

                def flaky_save(report_path, report):
                    saves.append(report)
                    if len(saves) >= 2:
                        raise fatal
                    return original_save(report_path, report)

                measured = {"status": "passed", "metrics": {"elapsed_seconds": 0.1}}

                def execute_case(fixture, repetition):
                    if fixture["id"] == "first":
                        return measured
                    state["cancel"] = True
                    if original_name == "BenchmarkCancelled":
                        raise BenchmarkCancelled("cancelled")
                    raise RuntimeError("synthetic worker failure")

                def execute_batch(pending):
                    yield {"fixture_id": "first", "repetition": 1, **measured}
                    state["cancel"] = True
                    if original_name == "BenchmarkCancelled":
                        raise BenchmarkCancelled("cancelled")
                    raise RuntimeError("synthetic worker failure")

                def callback():
                    if lifecycle == "batch":
                        runner.run_benchmark_batch(man, env, path, state, execute_batch)
                    else:
                        runner.run_benchmark_repetitions(man, env, path, state, execute_case)

                propagated = None
                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir), \
                     patch.object(runner, "save_benchmark_report", side_effect=flaky_save):
                    try:
                        core_module._run_claimed_background_task(key, callback)
                    except BaseException as exc:
                        propagated = exc
                saved = json.loads(Path(path).read_text()) if Path(path).exists() else {"cases": []}
                return list_runs(history_dir), state, saved, propagated
        finally:
            core_module.process_state.pop(key, None)

    def test_cancellation_publication_fatal_and_oserror_across_lifecycles(self):
        for lifecycle in ("repetitions", "batch"):
            for original_name in ("RuntimeError", "BenchmarkCancelled"):
                fatal = SystemExit("synthetic cancellation publication shutdown")
                records, state, saved, propagated = self.run_publication_failure_through_core(
                    lifecycle, original_name, fatal)
                with self.subTest(lifecycle=lifecycle, original=original_name, fatal="SystemExit"):
                    self.assertIsInstance(propagated, SystemExit)
                    self.assertEqual("failed", state["status"])
                    self.assertEqual(["done", "failed"], [t["status"] for t in state["tasks"]])
                    self.assertEqual(1, len(records))
                    self.assertEqual("failed", records[0]["status"])
                    self.assertEqual([{"fixture_id": "first", "repetition": 1, "status": "passed",
                                        "metrics": {"elapsed_seconds": 0.1}}], saved["cases"])
                fatal = OSError("synthetic cancellation publication disk full")
                records, state, saved, propagated = self.run_publication_failure_through_core(
                    lifecycle, original_name, fatal)
                with self.subTest(lifecycle=lifecycle, original=original_name, fatal="OSError"):
                    self.assertIsNone(propagated)  # core catches Exception
                    self.assertEqual("failed", state["status"])
                    self.assertEqual(1, len(records))
                    self.assertEqual("failed", records[0]["status"])
                    self.assertIn("synthetic cancellation publication disk full", records[0]["error"])
                    self.assertEqual([{"fixture_id": "first", "repetition": 1, "status": "passed",
                                        "metrics": {"elapsed_seconds": 0.1}}], saved["cases"])


class ShutdownPropagationTests(unittest.TestCase):
    def run_through_core(self, run_name, stage, worker_name, error):
        import core as core_module
        from run_history import list_runs
        key = "_test_1789_shutdown_history"
        fixtures = [{"id": "first", "sha256": "a"}]
        man = manifest(stage, fixtures)
        man["settings"] = {"local_python": sys.executable}
        core_module.process_state[key] = {"running": True, "logs": [], "process": None, "status": "running",
                                          "tasks": [{"fixture_id": "first", "status": "running"}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history_dir = str(Path(tmp) / "history")
                path = str(Path(tmp) / "report.json")
                state = core_module.process_state[key]
                if run_name == "batch":
                    def callback():
                        def interrupted(_pending):
                            raise error
                        runner.run_benchmark_batch(man, environment(), path, state, interrupted)
                else:
                    def callback():
                        with patch.object(runner, "load_app_config", return_value={}), \
                             patch.object(runner, "_validate_preparer_fixture"), \
                             patch.object(runner, worker_name, side_effect=error):
                            runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
                with patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
                    with self.assertRaises(type(error)):
                        core_module._run_claimed_background_task(key, callback)
                records = list_runs(history_dir)
                return records, state
        finally:
            core_module.process_state.pop(key, None)

    def test_batch_system_exit_and_keyboard_interrupt_fail_state_and_history(self):
        for error in (SystemExit("synthetic worker shutdown"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                records, state = self.run_through_core("batch", "script_review", None, error)
                self.assertEqual(1, len(records))
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", records[0]["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])

    def test_repetitions_system_exit_and_keyboard_interrupt_fail_state_and_history(self):
        for error in (SystemExit("synthetic worker shutdown"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                records, state = self.run_through_core(
                    "run_preparer_benchmark", "voicelab_preparer", "_run_preparer_worker", error)
                self.assertEqual(1, len(records))
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", records[0]["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])

    def test_generator_exit_propagates_and_fails_history(self):
        for run_name, stage, worker_name in (
                ("batch", "script_review", None),
                ("run_preparer_benchmark", "voicelab_preparer", "_run_preparer_worker")):
            with self.subTest(path=run_name):
                records, state = self.run_through_core(
                    run_name, stage, worker_name, GeneratorExit("synthetic generator exit"))
                self.assertEqual(1, len(records))
                self.assertEqual("failed", state["status"])
                self.assertEqual("failed", records[0]["status"])
                self.assertEqual("failed", state["tasks"][0]["status"])

    def test_shutdown_under_cancel_still_propagates_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = [{"id": "first", "sha256": "a"}]
            man = manifest("voicelab_preparer", fixtures)
            path = str(Path(tmp) / "report.json")
            state = fresh_state(fixtures)
            state["cancel"] = True
            # Pre-queued cancel normally returns before execute; queue it
            # during execute instead by using a worker that sets it.
            state["cancel"] = False

            def worker(*args):
                state["cancel"] = True
                raise SystemExit("synthetic shutdown under cancel")

            with patch.object(runner, "load_app_config", return_value={}), \
                 patch.object(runner, "_validate_preparer_fixture"), \
                 patch.object(runner, "_run_preparer_worker", side_effect=worker):
                with self.assertRaises(SystemExit):
                    runner.run_preparer_benchmark(man, environment(), path, state, "unused", tmp)
            self.assertEqual("failed", state["status"])
            self.assertEqual("failed", state["tasks"][0]["status"])


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
