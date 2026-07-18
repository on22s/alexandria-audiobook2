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
        status.assert_called_once_with("tnr-0", "remote-model")
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


if __name__ == "__main__":
    unittest.main()
