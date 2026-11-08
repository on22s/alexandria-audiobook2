"""Cached benchmark choices remain bounded by current runtime safety limits."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import llm_bench


class _ControlledClock:
    """Deterministic clock for llm_bench.time (patched object only).

    The first calling thread is the benchmark owner (the thread running
    find_optimal_concurrency's measure_level/measure_throughput owner-side
    timestamps). Only the owner's time() advances, by a fixed 10ms tick
    under a lock; worker threads (ThreadPoolExecutor _one_call) return the
    current value without advancing it. So the real measure_throughput's two
    owner timestamps always differ by exactly one tick regardless of worker
    scheduling, the aggregate wall is positive and fixed, worker elapsed may
    be 0 (valid, unused in the aggregate wall), and observer timestamps stay
    monotonic. This is fake time for cache-correctness tests only: it proves
    nothing about real model throughput, real clocks, or scheduler
    performance.
    """

    def __init__(self):
        self._now = 1000.0
        self._lock = threading.Lock()
        self._owner_id = None

    def time(self):
        with self._lock:
            thread_id = threading.get_ident()
            if self._owner_id is None:
                self._owner_id = thread_id
            if thread_id == self._owner_id:
                self._now += 0.01
                return self._now
            return self._now


class _BenchmarkClient:
    """Deterministic completions client exercising the real find sweep."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.create_calls = 0
        self._lock = threading.Lock()

        def create(**_kwargs):
            with self._lock:
                self.create_calls += 1
            if not self.healthy:
                raise RuntimeError("synthetic completion outage")
            # No real sleep: wall timing comes from _ControlledClock (owner
            # ticks), so throughput selection is scheduler-independent.
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="abcd" * 8))],
                usage=SimpleNamespace(completion_tokens=32))

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    def with_options(self, **_kwargs):
        return self


_STATUS = {"available": True, "loaded": True, "parallel": 8, "context_length": 8192,
           "runtime": "llama.cpp"}


def _valid_find(value):
    """A find_optimal_concurrency stand-in reporting a valid level-1 sample."""
    def find(_client, _model, _max_concurrency, server_parallel_limit=None, on_measurement=None):
        if on_measurement is not None:
            on_measurement(1, 10.0, 0.01)
        return value
    return find


class LlmConcurrencyCacheTests(unittest.TestCase):
    def test_invalid_cached_measurements_are_not_returned_or_used_as_limits(self):
        for cached in ("2", 1.5, -1, 0, True, [], {}):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text(json.dumps({"llm_local": {
                    "concurrency": cached, "concurrency_for": "http://localhost:1234/v1::model"}}))
                with patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")), \
                     patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(2)) as bench:
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
            with patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(8)), \
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
                 patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(2)):
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
                 patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(2)) as bench:
                got = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model",
                    client, max_concurrency=3, status={"available": True, "parallel": 4})
            self.assertEqual(2, got)
            self.assertEqual(1, bench.call_count)
            self.assertEqual((client, "model", 3), bench.call_args.args)
            self.assertEqual(4, bench.call_args.kwargs["server_parallel_limit"])
            self.assertTrue(callable(bench.call_args.kwargs["on_measurement"]))
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
                     patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(2)):
                    llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(), status=initial)
                before = json.loads(path.read_text())["llm_local"]
                current = dict(initial)
                if field not in ("gpu", "backend", "host"):
                    current[field] = changed
                with patch.object(llm_bench.platform, "node", return_value=changed if field == "host" else "host"), \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend", return_value=(
                         changed if field == "gpu" else "GPU", changed if field == "backend" else "backend")), \
                     patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(3)) as bench:
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
                 patch.object(llm_bench, "find_optimal_concurrency", side_effect=_valid_find(2)) as bench:
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


class LlmUnmeasuredFallbackCacheTests(unittest.TestCase):
    """Failed level-1 measurements must not become a measured cache entry.

    The principal test uses the actual find_optimal_concurrency and the
    production config load/write path; only the completions client, the
    status/GPU seams, and wall timing are deterministic fixtures.
    """

    def _call(self, path, client, status=None, **kwargs):
        with patch.object(llm_bench, "time", _ControlledClock()), \
             patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                          return_value=("fixture-GPU", "fixture-backend")), \
             patch.object(llm_bench.platform, "node", return_value="fixture-host"):
            return llm_bench.get_cached_or_benchmarked_concurrency(
                str(path), "local", "http://localhost:1234/v1", "model", client,
                max_concurrency=2, status=dict(_STATUS) if status is None else status, **kwargs)

    def test_failed_level1_returns_one_without_cache_then_rebenchmarks_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            before = path.read_bytes()
            failing = _BenchmarkClient(healthy=False)
            result = self._call(path, failing)
            self.assertEqual(1, result)
            self.assertIs(type(result), int)
            self.assertGreaterEqual(failing.create_calls, 1)
            self.assertEqual(before, path.read_bytes(),
                             "unmeasured fallback must not persist or overwrite profile bytes")
            healthy = _BenchmarkClient(healthy=True)
            self.assertEqual(2, self._call(path, healthy))
            self.assertEqual(9, healthy.create_calls,
                             "level 1 (3 trials) plus level 2 (3 trials x 2 requests)")
            saved = json.loads(path.read_text())["llm_local"]
            self.assertEqual(2, saved["concurrency"])
            self.assertEqual("http://localhost:1234/v1::model", saved["concurrency_for"])
            self.assertIsInstance(saved["concurrency_environment"], dict)
            third = _BenchmarkClient(healthy=True)
            self.assertEqual(2, self._call(path, third))
            self.assertEqual(0, third.create_calls,
                             "qualified result may be reused without re-benchmarking")
            self.assertEqual(2, json.loads(path.read_text())["llm_local"]["concurrency"])

    def test_genuine_measured_one_is_cacheable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            with patch.object(llm_bench, "measure_throughput",
                              side_effect=[10, 10, 10, 10, 10, 10]) as measure, \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")):
                self.assertEqual(1, llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    max_concurrency=2, status=dict(_STATUS)))
            self.assertEqual(6, measure.call_count)
            self.assertEqual(1, json.loads(path.read_text())["llm_local"]["concurrency"])
            with patch.object(llm_bench, "measure_throughput") as measure, \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")):
                self.assertEqual(1, llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    max_concurrency=2, status=dict(_STATUS)))
            measure.assert_not_called()

    def test_failed_higher_level_after_valid_level1_persists_measured_best(self):
        cases = [
            # (samples, expected best, decision sample count, max_concurrency)
            ([10, 10, 10, None], 1, 4, 4),
            ([10, 10, 10, 20, 20, 20, None], 2, 7, 8),
        ]
        for samples, expected, sample_count, cap in cases:
            with self.subTest(expected=expected, samples=samples), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text("{}")
                with patch.object(llm_bench, "measure_throughput", side_effect=list(samples)) as measure, \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")):
                    self.assertEqual(expected, llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=cap, status=dict(_STATUS)))
                self.assertEqual(sample_count, measure.call_count)
                self.assertEqual(expected, json.loads(path.read_text())["llm_local"]["concurrency"])
                with patch.object(llm_bench, "measure_throughput") as measure, \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")):
                    self.assertEqual(expected, llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=cap, status=dict(_STATUS)))
                measure.assert_not_called()

    def test_ambiguous_version1_concurrency_one_is_remeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            status = dict(_STATUS)
            old_env = llm_bench.get_concurrency_environment(
                "http://localhost:1234/v1", "model", False, None,
                "fixture-host", "fixture-GPU", "fixture-backend", status)
            old_env["version"] = 1
            path.write_text(json.dumps({"llm_local": {
                "concurrency": 1, "concurrency_for": "http://localhost:1234/v1::model",
                "concurrency_environment": old_env, "unrelated": "kept"}}))
            healthy = _BenchmarkClient(healthy=True)
            self.assertEqual(2, self._call(path, healthy, status=status))
            self.assertEqual(9, healthy.create_calls,
                             "pre-fix v1 entry is ambiguous and must not be trusted")
            saved = json.loads(path.read_text())["llm_local"]
            self.assertEqual(2, saved["concurrency"])
            self.assertNotEqual(1, saved["concurrency_environment"]["version"])
            self.assertEqual("kept", saved["unrelated"])

    def test_interruption_after_valid_level1_propagates_without_cache_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            before = path.read_bytes()
            with patch.object(llm_bench, "measure_throughput",
                              side_effect=[10, 10, 10, KeyboardInterrupt("cancelled")]), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture", "fixture")):
                with self.assertRaises(KeyboardInterrupt):
                    llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=4, status=dict(_STATUS))
            self.assertEqual(before, path.read_bytes())

    def test_failed_remeasurement_for_new_environment_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            self.assertEqual(2, self._call(path, _BenchmarkClient(healthy=True)))
            qualified = path.read_bytes()
            new_status = dict(_STATUS, parallel=4)
            failing = _BenchmarkClient(healthy=False)
            self.assertEqual(1, self._call(path, failing, status=new_status))
            self.assertEqual(qualified, path.read_bytes(),
                             "failed remeasurement must preserve prior qualified bytes")
            healthy = _BenchmarkClient(healthy=True)
            self.assertEqual(2, self._call(path, healthy, status=new_status))
            self.assertEqual(9, healthy.create_calls)
            saved = json.loads(path.read_text())["llm_local"]
            self.assertEqual(4, saved["concurrency_environment"]["parallel"])

    def test_single_slot_policy_invalidates_only_matching_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            seed_env = llm_bench.get_concurrency_environment(
                "http://localhost:1234/v1", "model", False, None,
                "fixture-host", "fixture-GPU", "fixture-backend", dict(_STATUS))
            other_profile = {"concurrency": 5, "concurrency_for": "http://other::model",
                             "concurrency_environment": {"version": 2}, "unrelated": "other-kept"}
            path.write_text(json.dumps({
                "llm_local": {"concurrency": 8, "concurrency_for": "http://localhost:1234/v1::model",
                              "concurrency_environment": seed_env, "unrelated": "kept"},
                "llm_remote": dict(other_profile), "top_level": "kept"}))
            policy_status = dict(_STATUS, parallel=1)
            client = _BenchmarkClient(healthy=True)
            with patch.object(llm_bench, "find_optimal_concurrency") as bench, \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-GPU", "fixture-backend")) as gpu, \
                 patch.object(llm_bench.platform, "node", return_value="fixture-host"):
                result = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", client,
                    max_concurrency=2, status=policy_status)
            self.assertEqual(1, result)
            self.assertIs(type(result), int)
            bench.assert_not_called()
            self.assertEqual(0, client.create_calls)
            gpu.assert_not_called()
            saved_config = json.loads(path.read_text())
            saved = saved_config["llm_local"]
            self.assertNotIn("concurrency_environment", saved,
                             "policy must invalidate matching qualification, not save a measurement")
            self.assertEqual(8, saved["concurrency"],
                             "old numeric value may remain as unqualified diagnostic data")
            self.assertEqual("kept", saved["unrelated"])
            self.assertEqual(other_profile, saved_config["llm_remote"])
            self.assertEqual("kept", saved_config["top_level"])
            healthy = _BenchmarkClient(healthy=True)
            self.assertEqual(2, self._call(path, healthy, status=dict(_STATUS, parallel=4)))
            self.assertEqual(9, healthy.create_calls,
                             "changed parallel must re-benchmark under normal qualification")
            saved = json.loads(path.read_text())["llm_local"]
            self.assertEqual(2, saved["concurrency"])
            self.assertEqual(4, saved["concurrency_environment"]["parallel"])

    def test_known_single_slot_invalidates_unknown_parallel_stale_cache(self):
        status = {"available": True, "loaded": True, "parallel": None,
                  "context_length": 8192, "runtime": "lmstudio"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"llm_local": {"unrelated": "kept"}}))
            with patch.object(llm_bench, "time", _ControlledClock()), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-GPU", "fixture-backend")) as gpu, \
                 patch.object(llm_bench.platform, "node", return_value="fixture-host"):
                with patch.object(llm_bench, "measure_throughput",
                                  side_effect=[10, 10, 10, 20, 20, 20]) as measure:
                    measured = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(status))
                self.assertEqual(2, measured)
                self.assertEqual(6, measure.call_count)
                saved = json.loads(path.read_text())["llm_local"]
                self.assertEqual(2, saved["concurrency"])
                self.assertIsNone(saved["concurrency_environment"]["parallel"])
                with patch.object(llm_bench, "measure_throughput",
                                  side_effect=AssertionError("policy must not measure")) as measure:
                    policy = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(status, parallel=1))
                self.assertEqual(1, policy)
                self.assertEqual(0, measure.call_count)
                saved = json.loads(path.read_text())["llm_local"]
                self.assertEqual(2, saved["concurrency"])
                self.assertEqual("kept", saved["unrelated"])
                original_find = llm_bench.find_optimal_concurrency
                with patch.object(llm_bench, "measure_throughput", return_value=None) as measure, \
                     patch.object(llm_bench, "find_optimal_concurrency", wraps=original_find) as find:
                    stale = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(status))
                self.assertEqual(1, stale,
                                 "a later unknown-parallel status must not reuse stale measured 2")
                self.assertEqual(1, find.call_count)
                self.assertEqual(1, measure.call_count)
                with patch.object(llm_bench, "measure_throughput",
                                  side_effect=[10, 10, 10, 20, 20, 20]) as measure:
                    retry = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(status))
                self.assertEqual(2, retry)
                self.assertEqual(6, measure.call_count)
                with patch.object(llm_bench, "measure_throughput") as measure:
                    third = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(status))
                self.assertEqual(2, third)
                measure.assert_not_called()

    def test_single_slot_with_no_matching_cache_writes_nothing(self):
        for profile in ({}, {"concurrency": 8, "concurrency_for": "http://other::model",
                             "concurrency_environment": {"version": 2}, "unrelated": "kept"}):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text(json.dumps({"llm_local": dict(profile)}))
                before = path.read_bytes()
                client = _BenchmarkClient(healthy=True)
                with patch.object(llm_bench, "find_optimal_concurrency") as bench, \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture-GPU", "fixture-backend")) as gpu:
                    result = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", client,
                        max_concurrency=2, status=dict(_STATUS, parallel=1))
                self.assertEqual(1, result)
                bench.assert_not_called()
                self.assertEqual(0, client.create_calls)
                gpu.assert_not_called()
                self.assertEqual(before, path.read_bytes())

    def test_single_slot_invalidation_rechecks_key_under_lock(self):
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            seed_env = llm_bench.get_concurrency_environment(
                "http://localhost:1234/v1", "model", False, None,
                "fixture-host", "fixture-GPU", "fixture-backend", dict(_STATUS))
            path.write_text(json.dumps({"llm_local": {
                "concurrency": 8, "concurrency_for": "http://localhost:1234/v1::model",
                "concurrency_environment": seed_env}}))
            newer = {"llm_local": {"concurrency": 99, "concurrency_for": "http://newer::model",
                                   "concurrency_environment": {"version": 2, "new": True},
                                   "unrelated": "new"}}
            @contextlib.contextmanager
            def fake_lock(_path):
                path.write_text(json.dumps(newer))
                yield
            with patch.object(llm_bench, "file_lock", side_effect=fake_lock), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-GPU", "fixture-backend")) as gpu:
                result = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    max_concurrency=2, status=dict(_STATUS, parallel=1))
            self.assertEqual(1, result)
            gpu.assert_not_called()
            self.assertEqual(newer, json.loads(path.read_text()),
                             "freshly changed evidence must not be deleted on stale authority")

    def test_single_slot_invalidation_error_is_best_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            seed_env = llm_bench.get_concurrency_environment(
                "http://localhost:1234/v1", "model", False, None,
                "fixture-host", "fixture-GPU", "fixture-backend", dict(_STATUS))
            path.write_text(json.dumps({"llm_local": {
                "concurrency": 8, "concurrency_for": "http://localhost:1234/v1::model",
                "concurrency_environment": seed_env}}))
            before = path.read_bytes()
            with patch.object(llm_bench, "atomic_json_write", side_effect=OSError("synthetic")), \
                 patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                              return_value=("fixture-GPU", "fixture-backend")) as gpu:
                result = llm_bench.get_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    max_concurrency=2, status=dict(_STATUS, parallel=1))
            self.assertEqual(1, result)
            gpu.assert_not_called()
            self.assertEqual(before, path.read_bytes(),
                             "failed invalidation must retain original bytes, not claim deletion")

    def test_nonpositive_initial_robust_throughput_is_unmeasured_fallback(self):
        for throughput in (0, -3.5):
            with self.subTest(throughput=throughput), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                path.write_text("{}")
                before = path.read_bytes()
                with patch.object(llm_bench, "_measure_robust", return_value=throughput) as robust, \
                     patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                  return_value=("fixture", "fixture")):
                    result = llm_bench.get_cached_or_benchmarked_concurrency(
                        str(path), "local", "http://localhost:1234/v1", "model", object(),
                        max_concurrency=2, status=dict(_STATUS))
                self.assertEqual(1, result)
                self.assertIs(type(result), int)
                robust.assert_called_once()
                self.assertEqual(before, path.read_bytes(),
                                 "nonpositive initial throughput must not become a measured cache entry")

    def test_persistence_error_is_best_effort_for_valid_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text("{}")
            before = path.read_bytes()
            healthy = _BenchmarkClient(healthy=True)
            with patch.object(llm_bench, "atomic_json_write", side_effect=OSError("synthetic")):
                self.assertEqual(2, self._call(path, healthy))
            self.assertEqual(9, healthy.create_calls)
            self.assertEqual(before, path.read_bytes())


class StandaloneBenchmarkClientTests(unittest.TestCase):
    def test_cli_key_reference_reaches_real_requests_through_configured_client(self):
        import contextlib
        import io
        import os
        import sys
        import httpx2
        import llm_provider
        from openai import OpenAI
        requests = []
        clients = []
        def handle(request):
            requests.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
            return httpx2.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Known benchmark completion."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
        def make_sdk(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
            clients.append(client)
            return client
        with patch.dict(os.environ, {"CLI_FIXTURE_KEY": "fixture-secret"}), \
             patch.object(sys, "argv", ["llm_bench.py", "--base-url", "https://fixture.invalid/v1",
                "--api-key", "env:CLI_FIXTURE_KEY", "--model", "fixture-model", "--max-concurrency", "1"]), \
             patch.object(llm_bench, "OpenAI", side_effect=make_sdk, create=True), \
             patch.object(llm_provider, "OpenAI", side_effect=make_sdk), \
             patch.object(llm_bench, "time", _ControlledClock()), \
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
