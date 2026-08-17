import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_core
import benchmark_runner


class BenchmarkRunnerTests(unittest.TestCase):
    def test_remote_clone_assets_are_staged_once_and_payload_is_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "ref.wav")
            ref.write_bytes(b"audio")
            digest = hashlib.sha256(b"audio").hexdigest()
            payload = {"fixtures": [
                {"voice_type": "clone", "ref_audio": "ref.wav",
                 "ref_audio_sha256": digest},
                {"voice_type": "clone", "ref_audio": "ref.wav",
                 "ref_audio_sha256": digest}]}
            completed = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(benchmark_runner.subprocess, "run",
                              return_value=completed) as run:
                staged = benchmark_runner._stage_remote_tts_assets(
                    payload, tmp, "tnr-0")
        self.assertEqual(2, run.call_count)
        self.assertEqual("ref.wav", payload["fixtures"][0]["ref_audio"])
        self.assertEqual(
            f"/tmp/alexandria-tts-benchmark-assets/{digest}.wav",
            staged["fixtures"][0]["ref_audio"])

    def test_tts_fixture_drift_is_rejected_before_worker(self):
        with self.assertRaisesRegex(ValueError, "hash changed"):
            benchmark_runner._validate_tts_fixture({
                "id": "tts", "sha256": "stale", "text": "Hello",
                "instruct": "Neutral", "speaker": "N", "voice": "Ryan", "seed": 0})

    def test_lora_training_fixture_drift_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "hash changed"):
            benchmark_runner._validate_lora_training_fixture({
                "id": "train", "sha256": "stale", "dataset_path": "dataset",
                "metadata_sha256": "x", "sample_count": 1, "audio_sha256": {},
                "epochs": 1, "seed": 42, "lr": 1e-6, "lora_r": 8,
                "lora_alpha": 16, "grad_accum": 1, "language": "english"}, ".")

    def test_preparer_fixture_drift_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "hash changed"):
            benchmark_runner._validate_preparer_fixture({
                "id": "prep", "sha256": "stale", "audio_path": "audio.wav",
                "audio_sha256": "x", "limit": 1, "language": "en",
                "model_revision": "revision"}, ".")

    def test_dedup_fixture_drift_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "hash changed"):
            benchmark_runner._validate_dedup_fixture({
                "id": "dedup", "sha256": "stale", "dataset_path": "dataset",
                "metadata_sha256": "x", "samples_per_volume": 2,
                "audio_sha256": {}, "model_id": "model", "seed": 42}, ".")

    def test_remote_lora_adapter_is_staged_once_and_payload_is_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "adapter")
            adapter.mkdir()
            hashes = {}
            for name in ("adapter_config.json", "adapter_model.safetensors",
                         "ref_sample.wav", "training_meta.json"):
                content = name.encode()
                Path(adapter, name).write_bytes(content)
                hashes[name] = hashlib.sha256(content).hexdigest()
            payload = {"fixtures": [
                {"voice_type": "lora", "adapter_path": "adapter",
                 "adapter_artifact_sha256": hashes},
                {"voice_type": "lora", "adapter_path": "adapter",
                 "adapter_artifact_sha256": hashes}]}
            completed = type("Result", (), {
                "returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(benchmark_runner.subprocess, "run",
                              return_value=completed) as run:
                staged = benchmark_runner._stage_remote_tts_assets(
                    payload, tmp, "tnr-0")
        self.assertEqual(6, run.call_count)
        self.assertEqual("adapter", payload["fixtures"][0]["adapter_path"])
        self.assertTrue(staged["fixtures"][0]["adapter_path"].startswith(
            "/tmp/alexandria-tts-benchmark-assets/lora-"))

    def test_network_rtt_probe_returns_elapsed_seconds(self):
        class FakeModels:
            def list(self):
                return None

        class FakeClient:
            models = FakeModels()

        rtt = benchmark_runner._measure_llm_network_rtt(FakeClient())
        self.assertIsInstance(rtt, float)
        self.assertGreaterEqual(rtt, 0.0)

    def test_network_rtt_probe_returns_none_when_probe_fails(self):
        class FakeModels:
            def list(self):
                raise RuntimeError("unreachable")

        class FakeClient:
            models = FakeModels()

        self.assertIsNone(benchmark_runner._measure_llm_network_rtt(FakeClient()))

    def test_runner_persists_each_successful_repetition(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp, "fixture.txt")
            fixture_path.write_text("one two three four five", encoding="utf-8")
            digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
            manifest = {"schema_version": 1, "stage": "script_generation",
                        "targets": ["local"], "repetitions": 2,
                        "fixtures": [{"id": "fixture", "sha256": digest,
                                      "path": str(fixture_path)}]}
            environment = benchmark_core.build_environment_fingerprint("local", {
                "hostname": "host", "gpu_name": "gpu", "backend": "rocm",
                "python_version": "3.10", "git_commit": "abc"})
            state = {"cancel": False, "logs": [],
                     "tasks": [{"fixture_id": "fixture", "status": "pending"}]}
            entries = [{"speaker": "NARRATOR", "text": "one two three four five",
                        "instruct": "Neutral."}]
            with patch.object(benchmark_runner, "load_app_config", return_value={
                    "llm_local": {"model_name": "model", "base_url": "http://local"}}), \
                 patch.object(benchmark_runner, "get_lmstudio_status", return_value={
                     "available": True, "loaded": True, "context_length": 8192}), \
                 patch.object(benchmark_runner, "OpenAI"), \
                 patch.object(benchmark_runner, "process_chunk", return_value=entries):
                report = benchmark_runner.run_script_generation_benchmark(
                    manifest, environment, str(Path(tmp, "report.json")), state,
                    str(Path(tmp, "config.json")), tmp)
            self.assertEqual(2, len(report["cases"]))
            self.assertTrue(all(case["status"] == "passed" for case in report["cases"]))
            self.assertEqual("complete", state["status"])
            self.assertEqual("done", state["tasks"][0]["status"])
            self.assertIn("network_rtt_seconds", report)

    def test_fixture_hash_change_fails_before_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "fixture.txt")
            path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash changed"):
                benchmark_runner._load_text_fixture(
                    {"id": "fixture", "sha256": "old", "path": str(path)}, tmp)

    def test_fixture_outside_uploads_is_rejected(self):
        with tempfile.TemporaryDirectory() as uploads, tempfile.TemporaryDirectory() as other:
            path = Path(other, "fixture.txt")
            path.write_text("text", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside uploads"):
                benchmark_runner._load_text_fixture(
                    {"id": "fixture", "sha256": "unused", "path": str(path)}, uploads)

    def test_thunder_target_uses_remote_profile_and_status(self):
        config = {"llm_remote_ssh": "tnr-0", "llm_remote": {
            "model_name": "remote-model", "base_url": "http://thunder/v1"}}
        with patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                "available": True, "loaded": True, "context_length": 65536}) as status, \
             patch.object(benchmark_runner, "get_lmstudio_status") as local_status:
            llm, observed = benchmark_runner._get_llm_benchmark_target(
                config, "thunder")
        status.assert_called_once_with("tnr-0", "remote-model", port=1234)
        local_status.assert_not_called()
        self.assertEqual("http://thunder/v1", llm["base_url"])
        self.assertEqual(65536, observed["context_length"])

    def test_thunder_target_fails_loudly_when_endpoint_is_unconfigured(self):
        with patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                "available": False, "loaded": False}):
            with self.assertRaisesRegex(ValueError, "endpoint is not configured"):
                benchmark_runner._get_llm_benchmark_target(
                    {"llm_remote": {}, "llm_remote_ssh": "tnr-0"}, "thunder")

    def test_review_runner_records_text_loss_and_change_metrics(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.json")
            original = [{"speaker": "NARRATOR", "text": "one two three",
                         "instruct": "Neutral."}]
            path.write_text(json.dumps(original), encoding="utf-8")
            from benchmark_fixtures import build_script_review_manifest
            manifest = build_script_review_manifest(
                [{"path": str(path), "entry_starts": [1]}], tmp)
            environment = benchmark_core.build_environment_fingerprint("local", {
                "hostname": "host", "gpu_name": "gpu", "backend": "rocm",
                "python_version": "3.10", "git_commit": "abc"})
            state = {"cancel": False, "logs": [],
                     "tasks": [{"fixture_id": "fixture", "status": "pending"}]}
            corrected = [{"speaker": "NARRATOR", "text": "one two three",
                          "instruct": "Calm."}]
            with patch.object(benchmark_runner, "load_app_config", return_value={
                    "llm_local": {"model_name": "model", "base_url": "http://local"}}), \
                 patch.object(benchmark_runner, "get_lmstudio_status", return_value={
                     "available": True, "loaded": True, "context_length": 8192}), \
                 patch.object(benchmark_runner, "OpenAI"), \
                 patch.object(benchmark_runner, "review_batch", return_value=corrected):
                report = benchmark_runner.run_script_review_benchmark(
                    manifest, environment, str(Path(tmp, "report.json")), state,
                    str(Path(tmp, "config.json")), tmp)
            self.assertEqual("passed", report["cases"][0]["status"])
            self.assertEqual(1.0, report["cases"][0]["quality"]["word_ratio"])
            self.assertEqual(1, report["cases"][0]["changes"]["instruct_changed"])
            self.assertIn("network_rtt_seconds", report)

    def test_pending_fixtures_with_repetitions_only_returns_missing(self):
        fixtures = [{"id": "a"}, {"id": "b"}]
        completed = {("a", 1)}
        pending = benchmark_runner._pending_fixtures_with_repetitions(
            fixtures, 2, completed)
        self.assertEqual(["a", "b"], [f["id"] for f in pending])
        self.assertEqual([2], pending[0]["repetition_numbers"])
        self.assertEqual([1, 2], pending[1]["repetition_numbers"])

    def test_pending_fixtures_with_repetitions_omits_fully_completed(self):
        pending = benchmark_runner._pending_fixtures_with_repetitions(
            [{"id": "a"}], 1, {("a", 1)})
        self.assertEqual([], pending)

    def test_remote_llm_base_url_extracts_configured_port(self):
        url = benchmark_runner._remote_llm_base_url(
            {"base_url": "https://uuid-1234.thundercompute.net:5555/v1"})
        self.assertEqual("http://localhost:5555/v1", url)

    def test_remote_llm_base_url_defaults_to_1234(self):
        url = benchmark_runner._remote_llm_base_url({"base_url": ""})
        self.assertEqual("http://localhost:1234/v1", url)

    def test_run_llm_worker_requires_remote_settings(self):
        with self.assertRaisesRegex(ValueError, "requires remote_root"):
            benchmark_runner._run_llm_worker("script_generation", {}, {}, "tnr-0")

    def test_run_llm_worker_builds_ssh_command_and_parses_marker(self):
        completed = type("Result", (), {
            "returncode": 0,
            "stdout": 'LLM_BENCHMARK_RESULT=[{"fixture_id":"f","repetition":1,"status":"passed"}]',
            "stderr": ""})()
        with patch.object(benchmark_runner.subprocess, "run",
                          return_value=completed) as run:
            cases = benchmark_runner._run_llm_worker(
                "nickname_detection", {"model_name": "m"},
                {"remote_root": "/remote", "remote_python": "/venv/python"}, "tnr-0")
        self.assertEqual([{"fixture_id": "f", "repetition": 1, "status": "passed"}], cases)
        command = run.call_args.args[0]
        self.assertEqual(["ssh", "tnr-0"], command[:2])
        self.assertIn("llm_benchmark_worker.py", command[2])
        self.assertIn("--stage nickname_detection", command[2])

    def test_run_llm_worker_raises_with_stderr_on_failure(self):
        completed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        with patch.object(benchmark_runner.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                benchmark_runner._run_llm_worker(
                    "nickname_detection", {}, {"remote_root": "/r", "remote_python": "/p"},
                    "tnr-0")

    def test_script_generation_thunder_target_dispatches_to_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp, "fixture.txt")
            fixture_path.write_text("one two three four five", encoding="utf-8")
            digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
            manifest = {"schema_version": 1, "stage": "script_generation",
                       "targets": ["thunder"], "repetitions": 1,
                       "settings": {"remote_root": "/remote", "remote_python": "/venv/python"},
                       "fixtures": [{"id": "fixture", "sha256": digest,
                                    "path": str(fixture_path)}]}
            environment = benchmark_core.build_environment_fingerprint("thunder", {
                "hostname": "host", "gpu_name": "gpu", "backend": "cuda",
                "python_version": "3.10", "git_commit": "abc"})
            state = {"cancel": False, "logs": [],
                    "tasks": [{"fixture_id": "fixture", "status": "pending"}]}
            worker_case = {"fixture_id": "fixture", "repetition": 1, "status": "passed"}
            with patch.object(benchmark_runner, "load_app_config", return_value={
                    "llm_remote": {"model_name": "model", "base_url": "http://thunder/v1"},
                    "llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                     "available": True, "loaded": True, "context_length": 8192}), \
                 patch.object(benchmark_runner, "OpenAI"), \
                 patch.object(benchmark_runner, "_run_llm_worker",
                              return_value=[worker_case]) as run_worker:
                report = benchmark_runner.run_script_generation_benchmark(
                    manifest, environment, str(Path(tmp, "report.json")), state,
                    str(Path(tmp, "config.json")), tmp)
            self.assertEqual([worker_case], report["cases"])
            self.assertEqual("complete", state["status"])
            run_worker.assert_called_once()
            self.assertEqual("script_generation", run_worker.call_args.args[0])
            payload = run_worker.call_args.args[1]
            self.assertEqual("http://localhost:1234/v1", payload["base_url"])
            self.assertEqual("one two three four five", payload["fixtures"][0]["text"])

    def test_script_review_thunder_target_dispatches_to_worker(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.json")
            original = [{"speaker": "NARRATOR", "text": "one two three",
                        "instruct": "Neutral."}]
            path.write_text(json.dumps(original), encoding="utf-8")
            from benchmark_fixtures import build_script_review_manifest
            manifest = build_script_review_manifest(
                [{"path": str(path), "entry_starts": [1]}], tmp, targets=["thunder"])
            manifest["settings"] = {"remote_root": "/remote", "remote_python": "/venv/python"}
            environment = benchmark_core.build_environment_fingerprint("thunder", {
                "hostname": "host", "gpu_name": "gpu", "backend": "cuda",
                "python_version": "3.10", "git_commit": "abc"})
            state = {"cancel": False, "logs": [],
                    "tasks": [{"fixture_id": "fixture", "status": "pending"}]}
            worker_case = {"fixture_id": manifest["fixtures"][0]["id"],
                           "repetition": 1, "status": "passed"}
            with patch.object(benchmark_runner, "load_app_config", return_value={
                    "llm_remote": {"model_name": "model", "base_url": "http://thunder/v1"},
                    "llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                     "available": True, "loaded": True, "context_length": 8192}), \
                 patch.object(benchmark_runner, "OpenAI"), \
                 patch.object(benchmark_runner, "_run_llm_worker",
                              return_value=[worker_case]) as run_worker:
                report = benchmark_runner.run_script_review_benchmark(
                    manifest, environment, str(Path(tmp, "report.json")), state,
                    str(Path(tmp, "config.json")), tmp)
            self.assertEqual([worker_case], report["cases"])
            payload = run_worker.call_args.args[1]
            self.assertEqual(original, payload["fixtures"][0]["original"])

    def test_persona_generation_thunder_target_dispatches_to_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {"schema_version": 1, "stage": "persona_generation",
                       "targets": ["thunder"], "repetitions": 1,
                       "settings": {"remote_root": "/remote", "remote_python": "/venv/python"},
                       "fixtures": [{"id": "f1", "entries": [], "speakers": ["A"],
                                    "batch_size": 1, "sha256": "x"}]}
            with patch.object(benchmark_runner, "_hash_entries", return_value="x"):
                environment = benchmark_core.build_environment_fingerprint("thunder", {
                    "hostname": "host", "gpu_name": "gpu", "backend": "cuda",
                    "python_version": "3.10", "git_commit": "abc"})
                state = {"cancel": False, "logs": [], "tasks": []}
                worker_case = {"fixture_id": "f1", "repetition": 1, "status": "passed"}
                with patch.object(benchmark_runner, "load_app_config", return_value={
                        "llm_remote": {"model_name": "model", "base_url": "http://thunder/v1"},
                        "llm_remote_ssh": "tnr-0"}), \
                     patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                         "available": True, "loaded": True, "context_length": 8192}), \
                     patch.object(benchmark_runner, "OpenAI"), \
                     patch.object(benchmark_runner, "_run_llm_worker",
                                  return_value=[worker_case]) as run_worker:
                    report = benchmark_runner.run_persona_generation_benchmark(
                        manifest, environment, str(Path(tmp, "report.json")), state,
                        str(Path(tmp, "config.json")), tmp)
            self.assertEqual([worker_case], report["cases"])
            run_worker.assert_called_once_with(
                "persona_generation", run_worker.call_args.args[1],
                manifest["settings"], "tnr-0")

    def test_nickname_detection_thunder_target_dispatches_to_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {"schema_version": 1, "stage": "nickname_detection",
                       "targets": ["thunder"], "repetitions": 1,
                       "settings": {"remote_root": "/remote", "remote_python": "/venv/python"},
                       "fixtures": [{"id": "f1", "entries": [], "expected_aliases": {},
                                    "existing_aliases": {}, "sha256": "x"}]}
            worker_case = {"fixture_id": "f1", "repetition": 1, "status": "passed"}
            with patch.object(benchmark_runner, "_hash_entries", return_value="x"), \
                 patch.object(benchmark_runner, "load_app_config", return_value={
                     "llm_remote": {"model_name": "model", "base_url": "http://thunder/v1"},
                     "llm_remote_ssh": "tnr-0"}), \
                 patch.object(benchmark_runner, "get_remote_lmstudio_status", return_value={
                     "available": True, "loaded": True, "context_length": 8192}), \
                 patch.object(benchmark_runner, "OpenAI"), \
                 patch.object(benchmark_runner, "_run_llm_worker",
                              return_value=[worker_case]) as run_worker:
                environment = benchmark_core.build_environment_fingerprint("thunder", {
                    "hostname": "host", "gpu_name": "gpu", "backend": "cuda",
                    "python_version": "3.10", "git_commit": "abc"})
                state = {"cancel": False, "logs": [], "tasks": []}
                report = benchmark_runner.run_nickname_detection_benchmark(
                    manifest, environment, str(Path(tmp, "report.json")), state,
                    str(Path(tmp, "config.json")), tmp)
            self.assertEqual([worker_case], report["cases"])
            run_worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
