import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import benchmark_environment
from routers import benchmark


def _tts_request(target, settings=None):
    return benchmark.BenchmarkPreflightRequest(manifest={
        "schema_version": 1, "stage": "tts_generation", "targets": [target],
        "fixtures": [{"id": "tts", "sha256": "abc"}],
        "settings": settings or {"remote_root": "/remote", "remote_python": "/venv/python"}})


def _request(targets=None):
    return benchmark.BenchmarkPreflightRequest(
        manifest={"schema_version": 1, "stage": "script_generation",
                  "targets": targets or ["local"],
                  "fixtures": [{"id": "chunk", "sha256": "abc"}]})


class BenchmarkApiTests(unittest.TestCase):
    def test_tts_preflight_uses_tts_environment_not_lmstudio(self):
        request = benchmark.BenchmarkPreflightRequest(manifest={
            "schema_version": 1, "stage": "tts_generation", "targets": ["thunder"],
            "fixtures": [{"id": "tts", "sha256": "abc"}],
            "settings": {"remote_root": "/remote", "remote_python": "/venv/python"}})
        with patch.object(benchmark, "load_app_config",
                          return_value={"llm_remote_ssh": "tnr-0"}), \
             patch.object(benchmark, "check_global_gpu_lock"), \
             patch.object(benchmark, "collect_thunder_tts_environment",
                          return_value={"target": "thunder", "sha256": "tts"}) as collect, \
             patch.object(benchmark, "collect_thunder_environment") as lmstudio:
            result = benchmark._build_benchmark_preflight(request)
        collect.assert_called_once_with(
            benchmark.ROOT_DIR, "tnr-0", "/remote", "/venv/python")
        lmstudio.assert_not_called()
        self.assertEqual("ready", result["benchmark_state"])

    def test_preflight_collects_each_requested_environment(self):
        config = {"llm_local": {"model_name": "local-model"},
                  "llm_remote": {"model_name": "remote-model"},
                  "llm_remote_ssh": "tnr-0"}
        with patch.object(benchmark, "load_app_config", return_value=config), \
             patch.object(benchmark, "check_global_gpu_lock") as lock, \
             patch.object(benchmark, "collect_local_environment",
                          return_value={"target": "local", "sha256": "one"}) as local, \
             patch.object(benchmark, "collect_thunder_environment",
                          return_value={"target": "thunder", "sha256": "two"}) as remote:
            result = benchmark._build_benchmark_preflight(
                _request(["local", "thunder"]))
        lock.assert_called_once_with("benchmark")
        local.assert_called_once_with(benchmark.ROOT_DIR, "local-model")
        remote.assert_called_once_with(benchmark.ROOT_DIR, "tnr-0", "remote-model", remote_root=None)
        self.assertEqual("ready", result["benchmark_state"])

    def test_preflight_rejects_mismatched_torch_builds_across_targets(self):
        config = {"llm_local": {"model_name": "local-model"},
                  "llm_remote": {"model_name": "remote-model"},
                  "llm_remote_ssh": "tnr-0"}
        with patch.object(benchmark, "load_app_config", return_value=config), \
             patch.object(benchmark, "check_global_gpu_lock"), \
             patch.object(benchmark, "collect_local_environment", return_value={
                 "target": "local", "sha256": "one",
                 "details": {"packages": {"torch": "2.10.0+rocm7.0"}}}), \
             patch.object(benchmark, "collect_thunder_environment", return_value={
                 "target": "thunder", "sha256": "two",
                 "details": {"packages": {"torch": "2.7.0+cu126"}}}):
            with self.assertRaisesRegex(ValueError, "different builds"):
                benchmark._build_benchmark_preflight(_request(["local", "thunder"]))

    def test_single_target_preflight_checks_against_persisted_baseline(self):
        """Real callers only ever request one target per preflight - the
        comparability check must fire against a PERSISTED baseline from an
        earlier single-target run, not only a same-call sibling."""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = str(Path(tmp, "environment_baselines.json"))
            with patch.object(benchmark, "ENVIRONMENT_BASELINE_PATH", baseline_path), \
                 patch.object(benchmark, "load_app_config", return_value={"llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark, "check_global_gpu_lock"), \
                 patch.object(benchmark, "collect_local_tts_environment", return_value={
                     "target": "local", "sha256": "one",
                     "details": {"packages": {"torch": "2.10.0+rocm7.0"}}}):
                benchmark._build_benchmark_preflight(_tts_request("local"))

            with patch.object(benchmark, "ENVIRONMENT_BASELINE_PATH", baseline_path), \
                 patch.object(benchmark, "load_app_config", return_value={"llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark, "check_global_gpu_lock"), \
                 patch.object(benchmark, "collect_thunder_tts_environment", return_value={
                     "target": "thunder", "sha256": "two",
                     "details": {"packages": {"torch": "2.7.0+cu126"}}}):
                with self.assertRaisesRegex(ValueError, "different builds"):
                    benchmark._build_benchmark_preflight(_tts_request("thunder"))

    def test_single_target_preflight_passes_with_matching_persisted_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = str(Path(tmp, "environment_baselines.json"))
            with patch.object(benchmark, "ENVIRONMENT_BASELINE_PATH", baseline_path), \
                 patch.object(benchmark, "load_app_config", return_value={"llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark, "check_global_gpu_lock"), \
                 patch.object(benchmark, "collect_local_tts_environment", return_value={
                     "target": "local", "sha256": "one",
                     "details": {"packages": {"torch": "2.10.0+rocm7.0"}}}):
                benchmark._build_benchmark_preflight(_tts_request("local"))

            with patch.object(benchmark, "ENVIRONMENT_BASELINE_PATH", baseline_path), \
                 patch.object(benchmark, "load_app_config", return_value={"llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark, "check_global_gpu_lock"), \
                 patch.object(benchmark, "collect_thunder_tts_environment", return_value={
                     "target": "thunder", "sha256": "two",
                     "details": {"packages": {"torch": "2.10.1+cu126"}}}):
                result = benchmark._build_benchmark_preflight(_tts_request("thunder"))
        self.assertEqual("ready", result["benchmark_state"])
        self.assertNotIn("stale_baseline", result)

    def test_single_target_preflight_flags_stale_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = str(Path(tmp, "environment_baselines.json"))
            old_environment = {"target": "local", "sha256": "one",
                               "details": {"packages": {"torch": "2.10.0+rocm7.0"}}}
            benchmark_environment.save_environment_baseline(
                "local", old_environment, baseline_path)
            # Backdate the saved baseline past the staleness threshold.
            import json
            with open(baseline_path) as f:
                baselines = json.load(f)
            baselines["local"]["collected_at"] = (
                time.time() - benchmark_environment.BASELINE_STALE_SECONDS - 1)
            with open(baseline_path, "w") as f:
                json.dump(baselines, f)

            with patch.object(benchmark, "ENVIRONMENT_BASELINE_PATH", baseline_path), \
                 patch.object(benchmark, "load_app_config", return_value={"llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark, "check_global_gpu_lock"), \
                 patch.object(benchmark, "collect_thunder_tts_environment", return_value={
                     "target": "thunder", "sha256": "two",
                     "details": {"packages": {"torch": "2.10.1+cu126"}}}):
                result = benchmark._build_benchmark_preflight(_tts_request("thunder"))
        self.assertTrue(result.get("stale_baseline"))

    def test_preflight_route_reports_validation_failure_as_400(self):
        request = _request()
        request.manifest["stage"] = "unknown"
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(benchmark.benchmark_preflight(request))
        self.assertEqual(400, raised.exception.status_code)

    def test_preflight_is_blocked_while_an_existing_gpu_task_runs(self):
        with patch.dict(benchmark.process_state["audio"], {"running": True}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                benchmark._build_benchmark_preflight(_request())
        self.assertIn("audio is currently running", raised.exception.detail)

    def test_status_never_exposes_live_process_objects(self):
        with patch.dict(benchmark.process_state["benchmark"],
                        {"running": True, "process": object(), "logs": ["running"]},
                        clear=True):
            status = asyncio.run(benchmark.benchmark_status())
        self.assertTrue(status["running"])
        self.assertNotIn("process", status)

    def test_start_rejects_stale_preflight_before_claiming_gpu(self):
        request = benchmark.BenchmarkStartRequest(
            **_request().model_dump(), preflight_id="stale")
        preflight = {"manifest": request.manifest, "environments": {},
                     "preflight_id": "fresh", "benchmark_state": "ready"}
        with patch.object(benchmark, "_build_benchmark_preflight",
                          return_value=preflight), \
             patch.object(benchmark, "claim_gpu_task") as claim:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(benchmark.benchmark_start(
                    benchmark.BackgroundTasks(), request))
        self.assertEqual(409, raised.exception.status_code)
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
