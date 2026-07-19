import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_environment


class BenchmarkEnvironmentTests(unittest.TestCase):
    def test_local_environment_combines_runtime_gpu_and_model_status(self):
        with patch.object(benchmark_environment, "get_runtime_info", return_value={
                "revision": "abc", "python": "3.10", "platform": {"system": "Linux"},
                "packages": {"torch": "2"}}), \
             patch.object(benchmark_environment, "get_gpu_name_and_backend",
                          return_value=("Local GPU", "rocm")), \
             patch.object(benchmark_environment, "get_lmstudio_status", return_value={
                 "available": True, "loaded": True, "context_length": 32768, "parallel": 1}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": True, "sha256": "tree-hash"}), \
             patch.object(benchmark_environment.platform, "node", return_value="local-host"):
            fingerprint = benchmark_environment.collect_local_environment("/repo", "model")
        self.assertEqual("local", fingerprint["target"])
        self.assertEqual("Local GPU", fingerprint["details"]["gpu_name"])
        self.assertEqual(32768, fingerprint["details"]["lmstudio"]["context_length"])
        self.assertEqual("tree-hash", fingerprint["details"]["worktree"]["sha256"])

    def test_local_worktree_identity_changes_with_untracked_source_content(self):
        completed = [
            type("Result", (), {"returncode": 0, "stdout": "?? app/new.py\n"})(),
            type("Result", (), {"returncode": 0, "stdout": b""})(),
            type("Result", (), {"returncode": 0, "stdout": "app/new.py\n"})(),
        ]
        with patch.object(benchmark_environment.subprocess, "run", side_effect=completed), \
             patch.object(benchmark_environment.Path, "is_file", return_value=True), \
             patch.object(benchmark_environment.Path, "read_bytes", return_value=b"first"):
            first = benchmark_environment._get_local_worktree_identity("/repo")
        completed[0] = type("Result", (), {"returncode": 0,
                                             "stdout": "?? app/new.py\n"})()
        completed[1] = type("Result", (), {"returncode": 0, "stdout": b""})()
        completed[2] = type("Result", (), {"returncode": 0,
                                             "stdout": "app/new.py\n"})()
        with patch.object(benchmark_environment.subprocess, "run", side_effect=completed), \
             patch.object(benchmark_environment.Path, "is_file", return_value=True), \
             patch.object(benchmark_environment.Path, "read_bytes", return_value=b"second"):
            second = benchmark_environment._get_local_worktree_identity("/repo")
        self.assertTrue(first["dirty"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_remote_runtime_uses_last_nonempty_line_after_banner(self):
        payload = {"hostname": "thunder", "python_version": "3.11",
                   "platform": {"system": "Linux"}}
        result = type("Result", (), {"returncode": 0,
                      "stdout": "decorative banner\n" + json.dumps(payload) + "\n",
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result) as run:
            observations = benchmark_environment._get_remote_runtime_observations("tnr-0")
        self.assertEqual(payload, observations)
        self.assertIn("python3 -c", run.call_args.args[1])

    def test_thunder_environment_fails_when_model_status_is_unavailable(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "def"}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": False}):
            with self.assertRaisesRegex(ValueError, "LM Studio status is unavailable"):
                benchmark_environment.collect_thunder_environment(
                    "/repo", "tnr-0", "model")

    def test_lmstudio_observations_reject_an_unloaded_model(self):
        with self.assertRaisesRegex(ValueError, "model is not loaded"):
            benchmark_environment._get_lmstudio_observations(
                {"available": True, "loaded": False}, "model")

    def test_verify_comparable_environments_rejects_torch_minor_mismatch(self):
        local_env = {"details": {"packages": {"torch": "2.10.0+rocm7.0"}}}
        thunder_env = {"details": {"packages": {"torch": "2.7.0+cu126"}}}
        with self.assertRaisesRegex(ValueError, "different builds"):
            benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_verify_comparable_environments_allows_matching_torch_minor(self):
        local_env = {"details": {"packages": {"torch": "2.10.0+rocm7.0"}}}
        thunder_env = {"details": {"packages": {"torch": "2.10.1+cu126"}}}
        benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_verify_comparable_environments_skips_fingerprints_without_torch(self):
        local_env = {"details": {"packages": {}}}
        thunder_env = {"details": {}}
        benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_baseline_round_trips_through_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            environment = {"target": "local", "details": {"packages": {"torch": "2.10.0"}}}
            benchmark_environment.save_environment_baseline("local", environment, path)
            loaded = benchmark_environment.load_environment_baseline("local", path)
        self.assertEqual(environment, loaded["environment"])
        self.assertIn("collected_at", loaded)

    def test_baseline_load_returns_none_when_target_never_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            self.assertIsNone(benchmark_environment.load_environment_baseline("thunder", path))

    def test_baseline_save_preserves_other_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            benchmark_environment.save_environment_baseline("local", {"a": 1}, path)
            benchmark_environment.save_environment_baseline("thunder", {"b": 2}, path)
            local_entry = benchmark_environment.load_environment_baseline("local", path)
        self.assertEqual({"a": 1}, local_entry["environment"])

    def test_baseline_staleness_uses_collected_at(self):
        fresh = {"collected_at": time.time()}
        stale = {"collected_at": time.time() - benchmark_environment.BASELINE_STALE_SECONDS - 1}
        self.assertFalse(benchmark_environment.is_baseline_stale(fresh))
        self.assertTrue(benchmark_environment.is_baseline_stale(stale))

    def test_verify_remote_checkout_accepts_clean_matching_tree(self):
        result = type("Result", (), {"returncode": 0, "stdout": "a" * 40 + "\n",
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            commit = benchmark_environment._verify_remote_checkout(
                "/repo", "tnr-0", "/remote")
        self.assertEqual("a" * 40, commit)

    def test_verify_remote_checkout_rejects_revision_mismatch(self):
        result = type("Result", (), {"returncode": 0, "stdout": "a" * 40 + "\n",
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "b" * 40}):
            with self.assertRaisesRegex(ValueError, "match the local git revision"):
                benchmark_environment._verify_remote_checkout("/repo", "tnr-0", "/remote")

    def test_verify_remote_checkout_rejects_dirty_tree(self):
        result = type("Result", (), {"returncode": 0,
                      "stdout": "a" * 40 + "\n M app/foo.py\n", "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "must be clean"):
                benchmark_environment._verify_remote_checkout("/repo", "tnr-0", "/remote")

    def test_thunder_environment_verifies_checkout_when_remote_root_given(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "def"}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": True, "loaded": True}), \
             patch.object(benchmark_environment, "_verify_remote_checkout",
                          return_value="abc") as verify:
            fingerprint = benchmark_environment.collect_thunder_environment(
                "/repo", "tnr-0", "model", remote_root="/remote")
        verify.assert_called_once_with("/repo", "tnr-0", "/remote")
        self.assertEqual("abc", fingerprint["details"]["remote_checkout_commit"])

    def test_thunder_environment_skips_checkout_check_without_remote_root(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "def"}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": True, "loaded": True}), \
             patch.object(benchmark_environment, "_verify_remote_checkout") as verify:
            fingerprint = benchmark_environment.collect_thunder_environment(
                "/repo", "tnr-0", "model")
        verify.assert_not_called()
        self.assertNotIn("remote_checkout_commit", fingerprint["details"])

    def test_thunder_tts_rejects_checkout_that_does_not_match(self):
        payload = {"hostname": "thunder", "python_version": "3.11",
                   "torch": "2.7", "qwen_tts": "1"}
        result = type("Result", (), {"returncode": 0,
                      "stdout": "banner\n" + json.dumps(payload) + "\n" + "d" * 40 + "\n",
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A100", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "match the local git revision"):
                benchmark_environment.collect_thunder_tts_environment(
                    "/repo", "tnr-0", "/remote", "/venv/python")


if __name__ == "__main__":
    unittest.main()
