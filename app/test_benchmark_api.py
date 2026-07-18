import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import benchmark


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
        remote.assert_called_once_with(benchmark.ROOT_DIR, "tnr-0", "remote-model")
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
