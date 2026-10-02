"""Cached benchmark choices remain bounded by current runtime safety limits."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import llm_bench


class LlmConcurrencyCacheTests(unittest.TestCase):
    def test_invalid_cached_measurements_are_not_returned_or_used_as_limits(self):
        for cached in ("2", 1.5, -1, 0, True, [], {}):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text(json.dumps({"llm_local": {
                    "concurrency": cached, "concurrency_for": "http://localhost:1234/v1::model"}}))
                with patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")), \
                     patch.object(llm_bench, "find_optimal_concurrency", return_value=2) as bench:
                    result = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status={"available": True, "parallel": 8})
                self.assertEqual(2, result)
                self.assertIs(type(result), int)
                bench.assert_called_once()
                self.assertEqual(2, json.loads(path.read_text())["llm_local"]["concurrency"])

    def test_matching_cache_obeys_live_server_and_caller_limits_without_rebenchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            status = {"available": True, "parallel": 8, "context_length": 8192}
            with patch.object(llm_bench, "find_optimal_concurrency", return_value=8), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")):
                llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(), status=status)
            for current, cap, expected in [(status, 3, 3), (status, 16, 8),
                                            ({"available": False}, 16, 1), (status, 16, 8)]:
                with self.subTest(status=current, cap=cap), \
                     patch.object(llm_bench, "find_optimal_concurrency") as bench, \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")) as gpu:
                    got = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model",
                        object(), max_concurrency=cap, status=current)
                    self.assertEqual(expected, got)
                    bench.assert_not_called()
                    if current.get("available"):
                        gpu.assert_called_once()
                    else:
                        gpu.assert_not_called()
            self.assertEqual(8, json.loads(path.read_text())["llm_local"]["concurrency"])

    def test_cache_hit_fetches_status_when_caller_has_not_supplied_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            current = {"available": True, "parallel": 2, "context_length": 8192,
                       "runtime": "llama.cpp"}
            with patch.object(llm_bench.lmstudio_settings, "get_remote_gpu_name_and_backend",
                              return_value=("fixture", "fixture")), \
                 patch.object(llm_bench, "find_optimal_concurrency", return_value=2):
                llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "remote", "http://remote:1234/v1", "model", object(),
                    ssh_alias="test-host", status=current)
            with patch.object(llm_bench.lmstudio_settings, "get_current_status",
                              return_value=current) as status, \
                 patch.object(llm_bench.lmstudio_settings, "get_remote_lmstudio_status", return_value=current), \
                 patch.object(llm_bench.lmstudio_settings, "get_remote_gpu_name_and_backend",
                              return_value=("fixture", "fixture")), \
                 patch.object(llm_bench, "find_optimal_concurrency") as bench:
                got = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "remote", "http://remote:1234/v1", "model",
                    object(), ssh_alias="test-host")
            self.assertEqual(2, got)
            status.assert_called_once_with("remote", "http://remote:1234/v1", "model", ssh_alias="test-host", api_key=None)
            bench.assert_not_called()

    def test_cache_miss_still_benchmarks_with_both_limits_and_persists_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            client = object()
            with patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")), \
                 patch.object(llm_bench, "find_optimal_concurrency", return_value=2) as bench:
                got = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model",
                    client, max_concurrency=3, status={"available": True, "parallel": 4})
            self.assertEqual(2, got)
            bench.assert_called_once_with(client, "model", 3, server_parallel_limit=4)
            saved = json.loads(path.read_text())["llm_local"]
            self.assertEqual(2, saved["concurrency"])
            self.assertEqual("http://localhost:1234/v1::model", saved["concurrency_for"])

    def test_runtime_changes_invalidate_real_saved_measurement_and_new_record_reuses(self):
        initial = {"available": True, "loaded": True, "parallel": 8,
                   "context_length": 8192, "runtime": "llama.cpp", "model_path": "/model.gguf",
                   "build": "build1"}
        changes = [("parallel", 4), ("context_length", 16384), ("runtime", "lmstudio"),
                   ("model_path", "/different.gguf"), ("build", "build2"),
                   ("gpu", "different GPU"), ("backend", "other backend"),
                   ("host", "different host")]
        for field, changed in changes:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text("{}")
                with patch.object(llm_bench.platform, "node", return_value="host"), \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("GPU", "backend")), \
                     patch.object(llm_bench, "find_optimal_concurrency", return_value=2):
                    llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(), status=initial)
                before = json.loads(path.read_text())["llm_local"]
                current = dict(initial)
                if field not in ("gpu", "backend", "host"):
                    current[field] = changed
                with patch.object(llm_bench.platform, "node", return_value=changed if field == "host" else "host"), \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend", return_value=(
                         changed if field == "gpu" else "GPU", changed if field == "backend" else "backend")), \
                     patch.object(llm_bench, "find_optimal_concurrency", return_value=3) as bench:
                    for _ in range(2):
                        self.assertEqual(3, llm_bench.get_cached_or_benchmarked_concurrency(
                            str(path), "local", "http://localhost:1234/v1", "model", object(), status=current))
                bench.assert_called_once()
                after = json.loads(path.read_text())["llm_local"]
                self.assertIsInstance(after["concurrency_environment"], dict)
                self.assertNotEqual(before["concurrency_environment"], after["concurrency_environment"])

    def test_legacy_measurement_without_environment_is_remeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"llm_local": {
                "concurrency": 8, "concurrency_for": "http://localhost:1234/v1::model"}}))
            with patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")), \
                 patch.object(llm_bench, "find_optimal_concurrency", return_value=2) as bench:
                self.assertEqual(2, llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    status={"available": True, "parallel": 8, "context_length": 8192}))
            bench.assert_called_once()
            self.assertIn("concurrency_environment", json.loads(path.read_text())["llm_local"])

    def test_unavailable_status_preserves_saved_measurement_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text('{"llm_local":{"concurrency":8,"concurrency_for":"other"}}')
            before = path.read_bytes()
            with patch.object(llm_bench, "find_optimal_concurrency") as bench, \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")) as gpu:
                self.assertEqual(1, llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    status={"available": False}))
            self.assertEqual(before, path.read_bytes())
            bench.assert_not_called()
            gpu.assert_not_called()


class LlmBenchmarkMeasurementTests(unittest.TestCase):
    @staticmethod
    def client(content, completion):
        usage = None if completion is None else SimpleNamespace(completion_tokens=completion)
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))], usage=usage)
        create = MagicMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **_kwargs: client
        return client

    def test_zero_invalid_or_empty_completions_do_not_measure_success(self):
        for content, completion in (("actual text", 0), ("actual text", -1),
                                    ("actual text", "12"), ("actual text", True),
                                    ("", 12), ("   ", None), (None, None)):
            with self.subTest(content=content, completion=completion):
                client = self.client(content, completion)
                with self.assertRaises(ValueError):
                    llm_bench._one_call(client, "model", 1600, 1)
                self.assertIsNone(llm_bench.measure_throughput(client, "model", 2, timeout=1))

    def test_valid_usage_and_nonempty_usage_less_reply_are_measured(self):
        for completion, expected in ((32, 32), (None, 4)):
            with self.subTest(completion=completion):
                client = self.client("abcd" * 4, completion)
                tokens, elapsed = llm_bench._one_call(client, "model", 1600, 1)
                self.assertEqual(expected, tokens)
                self.assertGreaterEqual(elapsed, 0)

    def test_zero_throughput_cannot_escalate_concurrency(self):
        with patch.object(llm_bench, "_measure_robust", return_value=0) as measure:
            self.assertEqual(1, llm_bench.find_optimal_concurrency(object(), "model", 16))
        measure.assert_called_once()
        with patch.object(llm_bench, "_measure_robust", side_effect=[10, 20, 0]) as measure:
            self.assertEqual(2, llm_bench.find_optimal_concurrency(object(), "model", 16))
        self.assertEqual([1, 2, 4], [call.args[2] for call in measure.call_args_list])


class StandaloneBenchmarkClientTests(unittest.TestCase):
    def test_cli_key_reference_reaches_real_requests_through_configured_client(self):
        import contextlib
        import io
        import os
        import sys
        import httpx
        import llm_provider
        from openai import OpenAI
        requests = []
        clients = []
        def handle(request):
            requests.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Known benchmark completion."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
        def make_sdk(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            clients.append(client)
            return client
        with patch.dict(os.environ, {"CLI_FIXTURE_KEY": "fixture-secret"}), \
             patch.object(sys, "argv", ["llm_bench.py", "--base-url", "https://fixture.invalid/v1",
                "--api-key", "env:CLI_FIXTURE_KEY", "--model", "fixture-model", "--max-concurrency", "1"]), \
             patch.object(llm_bench, "OpenAI", side_effect=make_sdk, create=True), \
             patch.object(llm_provider, "OpenAI", side_effect=make_sdk), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                llm_bench._main()
            finally:
                for client in clients:
                    client.close()
        self.assertEqual(1, len(clients))
        self.assertEqual(3, len(requests))
        for url, authorization, body in requests:
            self.assertEqual("https://fixture.invalid/v1/chat/completions", url)
            self.assertEqual("Bearer fixture-secret", authorization)
            self.assertEqual("fixture-model", body["model"])
            self.assertEqual(1600, body["max_tokens"])
            self.assertEqual(["system", "user"], [message["role"] for message in body["messages"]])
        self.assertIn("Optimal concurrency: 1", output.getvalue())
        self.assertNotIn("fixture-secret", output.getvalue())
