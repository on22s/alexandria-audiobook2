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
            completed = type("Result", (), {"returncode": 0, "stdout": "/tmp/alexandria-tts-benchmark-assets.abcdefghij\n", "stderr": ""})()
            with patch.object(benchmark_runner, "run_benchmark_subprocess",
                              return_value=completed) as run:
                staged = benchmark_runner._stage_remote_tts_assets(
                    payload, tmp, "tnr-0")
        self.assertEqual(2, run.call_count)
        self.assertEqual("ref.wav", payload["fixtures"][0]["ref_audio"])
        self.assertEqual(
            f"/tmp/alexandria-tts-benchmark-assets.abcdefghij/{digest}.wav",
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
                "returncode": 0, "stdout": "/tmp/alexandria-tts-benchmark-assets.abcdefghij\n", "stderr": ""})()
            with patch.object(benchmark_runner, "run_benchmark_subprocess",
                              return_value=completed) as run:
                staged = benchmark_runner._stage_remote_tts_assets(
                    payload, tmp, "tnr-0")
        self.assertEqual(6, run.call_count)
        self.assertEqual("adapter", payload["fixtures"][0]["adapter_path"])
        self.assertTrue(staged["fixtures"][0]["adapter_path"].startswith(
            "/tmp/alexandria-tts-benchmark-assets.abcdefghij/lora-"))

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
                 patch.object(benchmark_runner, "make_llm_client"), \
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
                 patch.object(benchmark_runner, "make_llm_client"), \
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
        with patch.object(benchmark_runner, "run_benchmark_subprocess",
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
        with patch.object(benchmark_runner, "run_benchmark_subprocess", return_value=completed):
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
                 patch.object(benchmark_runner, "make_llm_client"), \
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
            self.assertEqual("http://localhost:1234/v1", payload["llm_config"]["base_url"])
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
                 patch.object(benchmark_runner, "make_llm_client"), \
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
                     patch.object(benchmark_runner, "make_llm_client"), \
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
                 patch.object(benchmark_runner, "make_llm_client"), \
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


class RemoteProfilingHashOutputTests(unittest.TestCase):
    def test_remote_hash_uses_last_nonempty_line_after_ssh_banner(self):
        fixture = {'zip_path': 'fixture.zip', 'zip_sha256': 'zip-hash',
                   'model_sha256': 'a'*64}
        settings = {'remote_root': '/remote', 'remote_python': '/remote/python',
                    'remote_model_path': '/remote/model.gguf'}
        result_type = type('Result', (), {})
        def result(stdout='', returncode=0):
            value = result_type()
            value.stdout, value.stderr, value.returncode = stdout, '', returncode
            return value
        for output in ('a'*64+'  /remote/model.gguf\n',
                       'Thunder Compute banner\n\n'+'a'*64+'  /remote/model.gguf\n\n'):
            with self.subTest(output=output), patch.object(benchmark_runner, 'run_benchmark_subprocess', side_effect=[
                result(), result(output),
                result('PROFILING_BENCHMARK_RESULT={"status":"passed","metrics":{}}\n')]) as run:
                observed = benchmark_runner._run_profiling_worker(fixture, 'thunder', settings, '/local', 'fixture-host')
                self.assertEqual('passed', observed['status'])
                self.assertEqual(3, run.call_count)
        for output, status in (('Thunder banner\n'+'b'*64+' model\n', 0),
                               ('Thunder banner\n', 0), (' \n\n', 0),
                               ('a'*64+' model\n', 1)):
            with self.subTest(output=output, status=status), patch.object(
                benchmark_runner.subprocess, 'run', side_effect=[result(), result(output, status)]) as run:
                with self.assertRaisesRegex(ValueError, 'model hash'):
                    benchmark_runner._run_profiling_worker(fixture, 'thunder', settings, '/local', 'fixture-host')
                self.assertEqual(2, run.call_count)


class TTSBenchmarkCancellationTests(unittest.TestCase):
    def test_http_cancel_during_worker_preserves_measured_cases_and_resume(self):
        import copy
        import json
        import numpy as np
        import soundfile as sf
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import benchmark
        from tts_benchmark import measure_wav
        for target in ('local', 'thunder'):
            for cancel in (False, True):
                with self.subTest(target=target, cancel=cancel), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    fixture = {'id': 'voice', 'text': 'A measured sentence.',
                        'instruct': 'Calm.', 'speaker': 'ANN', 'voice': 'Ryan', 'seed': 7}
                    content = {key: fixture[key] for key in
                               ('text', 'instruct', 'speaker', 'voice', 'seed')}
                    fixture['sha256'] = benchmark_runner._hash_entries(content)
                    manifest = {'schema_version': 1, 'stage': 'tts_generation',
                        'targets': [target], 'repetitions': 2, 'fixtures': [fixture]}
                    environment = benchmark_core.build_environment_fingerprint(target, {
                        'hostname': 'cpu-fixture', 'gpu_name': 'unmeasured', 'backend': 'cpu',
                        'python_version': '3.10', 'git_commit': 'fixture'})
                    before = copy.deepcopy((manifest, environment))
                    state = {'running': True, 'cancel': False, 'logs': [],
                        'status': 'running', 'tasks': [{'fixture_id': 'voice', 'status': 'pending'}]}
                    app = FastAPI()
                    app.include_router(benchmark.router)
                    path = root / 'report.json'
                    config = root / 'config.json'
                    config.write_text(json.dumps({'tts': {'device': 'cpu'},
                                                  'llm_remote_ssh': 'fixture-host'}))
                    original_config = config.read_bytes()
                    pcm = np.sin(np.arange(2400) * 0.11).astype(np.float32) * 0.2
                    with patch.object(benchmark, 'process_state', {'benchmark': state}), TestClient(app) as client:
                        def worker(payload, selected_target, settings, root_dir, output_dir, ssh_alias):
                            self.assertTrue(state['running'])
                            self.assertEqual(target, selected_target)
                            self.assertEqual([1, 2], payload['fixtures'][0]['repetition_numbers'])
                            self.assertEqual('fixture-host', ssh_alias)
                            if cancel:
                                response = client.post('/api/benchmark/cancel')
                                self.assertEqual(200, response.status_code, response.text)
                                self.assertEqual({'status': 'cancel queued'}, response.json())
                                self.assertTrue(state['cancel'])
                            cases = []
                            for repetition in (1, 2):
                                audio = root / ('case' + str(repetition) + '.wav')
                                sf.write(audio, pcm, 24000, subtype='PCM_16')
                                cases.append({'fixture_id': 'voice', 'repetition': repetition,
                                    'status': 'passed', 'metrics': measure_wav(str(audio), 1.0)})
                            return cases
                        with patch.object(benchmark_runner, '_run_tts_worker', side_effect=worker) as dispatch:
                            report = benchmark_runner.run_tts_generation_benchmark(
                                manifest, environment, str(path), state, str(config), tmp)
                        dispatch.assert_called_once()
                        self.assertEqual('cancelled' if cancel else 'complete', state['status'])
                        self.assertEqual('done', state['tasks'][0]['status'])
                        self.assertEqual([1, 2], [case['repetition'] for case in report['cases']])
                        self.assertTrue(all(case['quality']['passed'] for case in report['cases']))
                        self.assertEqual(report, json.loads(path.read_text()))
                        for repetition, case in zip((1, 2), report['cases']):
                            audio = root / ('case' + str(repetition) + '.wav')
                            self.assertEqual(case['metrics'], measure_wav(str(audio), 1.0))
                        saved = path.read_bytes()
                        state.update(cancel=False, status='running')
                        with patch.object(benchmark_runner, '_run_tts_worker') as no_dispatch:
                            resumed = benchmark_runner.run_tts_generation_benchmark(
                                manifest, environment, str(path), state, str(config), tmp)
                        no_dispatch.assert_not_called()
                        self.assertEqual(report, resumed)
                        self.assertEqual(saved, path.read_bytes())
                        self.assertEqual('complete', state['status'])
                    self.assertEqual(before, (manifest, environment))
                    self.assertEqual(original_config, config.read_bytes())


class NicknameEmptyGoldScoringTests(unittest.TestCase):
    def test_known_negative_positive_and_false_positive_cases_preserve_raw_results(self):
        import copy
        import json
        cases = [({}, {}, {}, True, 1., 1., 1.),
                 ({}, {'Bri': 'Brian'}, {'Bri': ['invented']}, False, 0., 1., 1.),
                 ({'Bri': 'Brian'}, {}, {}, False, 0., 0., 0.),
                 ({'Bri': 'Brian'}, {'Bri': 'Brian'}, {'bri': ['evidence']}, True, 1., 1., 1.),
                 ({'Bri': 'Brian'}, {'Bri': 'Brian', 'Bob': 'Robert'}, {'Bri': ['evidence']}, False, .5, 1., 1.),
                 ({'Bri': 'Brian'}, {'Bri': 'Brian'}, {}, False, 1., 1., 0.)]
        for expected, aliases, evidence, passed, precision, recall, coverage in cases:
            with self.subTest(expected=expected, aliases=aliases, evidence=evidence):
                fixture = {'id': 'fixture', 'entries': [], 'existing_aliases': {}, 'expected_aliases': expected}
                before = copy.deepcopy((fixture, aliases, evidence))
                with patch.object(benchmark_runner, 'find_nicknames', return_value=(aliases, evidence)) as detector:
                    result = benchmark_runner._run_nickname_case(fixture, 1, object(), 'model', 4096, 1)
                detector.assert_called_once()
                self.assertEqual('passed' if passed else 'failed', result['status'])
                self.assertEqual({'passed': passed, 'precision': precision, 'recall': recall,
                                  'evidence_coverage': coverage}, result['quality'])
                self.assertEqual(aliases, result['aliases'])
                self.assertEqual(evidence, result['evidence'])
                self.assertEqual(before, (fixture, aliases, evidence))
                self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))


class PersonaEmptyFixtureTests(unittest.TestCase):
    def fixture(self, speakers):
        import copy
        content = {'entries': [{'speaker': 'NARRATOR', 'text': 'No dialogue.'}],
                   'speakers': copy.deepcopy(speakers), 'batch_size': 40}
        return {**content, 'id': 'fixture', 'sha256': benchmark_runner._hash_entries(content)}

    def test_runner_rejects_hash_valid_empty_or_malformed_speakers_before_work(self):
        import copy
        for speakers in ([], None, '', 'ALICE', [''], ['  '], [7]):
            with self.subTest(speakers=speakers):
                fixture = self.fixture(speakers)
                original = copy.deepcopy(fixture)
                with patch.object(benchmark_runner.generate_personas, '_discover_batch_characters',
                                  side_effect=AssertionError('invalid fixture reached discovery')) as discover, \
                     patch.object(benchmark_runner.generate_personas, '_compile_persona') as compile_persona:
                    with self.assertRaisesRegex(ValueError, 'persona speakers'):
                        benchmark_runner._run_persona_case(fixture, object(), 'model', 4096)
                    discover.assert_not_called()
                    compile_persona.assert_not_called()
                self.assertEqual(original, fixture)

    def test_actual_remote_worker_rejects_hash_valid_empty_fixture(self):
        import llm_benchmark_worker
        fixture = self.fixture([])
        fixture['repetition_numbers'] = [1]
        payload = {'base_url': 'http://fixture.invalid/v1', 'model_name': 'model',
                   'fixtures': [fixture]}
        with patch.object(llm_benchmark_worker, 'make_llm_client'), \
             patch.object(benchmark_runner.generate_personas, '_discover_batch_characters') as discovery:
            with self.assertRaisesRegex(ValueError, 'persona speakers'):
                llm_benchmark_worker.execute_payload('persona_generation', payload)
            discovery.assert_not_called()

    def test_builder_and_runner_share_speaker_validation(self):
        from benchmark_validation import validate_persona_speakers
        from benchmark_fixtures import build_persona_generation_manifest
        with self.assertRaisesRegex(ValueError, 'persona speakers'):
            build_persona_generation_manifest([{'entries': [{'speaker': 'NARRATOR', 'text': 'No dialogue.'}]}])
        speakers = ['ALICE', 'BOB']
        self.assertIsNone(validate_persona_speakers(speakers))
        self.assertEqual(['ALICE', 'BOB'], speakers)

    def test_real_case_scoring_preserves_positive_and_missing_persona_failure(self):
        import copy
        from benchmark_fixtures import build_persona_generation_manifest
        fixture = build_persona_generation_manifest([{'entries': [{'speaker': 'ALICE', 'text': 'Hello there.'}]}])['fixtures'][0]
        original = copy.deepcopy(fixture)
        characters = [{'name': 'ALICE', 'features': ['a calm voice'], 'sample_lines': ['Hello there.']}]
        def compile_fixture(client, model, engine, config, root, ref_dir, speaker, samples, *args, **kwargs):
            kwargs["preview_saver"](root, engine, config, speaker, 'Calm voice.', 'Hello there.')
        for compile_function, passed in ((compile_fixture, True), (lambda *args, **kwargs: None, False)):
            with self.subTest(passed=passed), \
                 patch.object(benchmark_runner.generate_personas, '_discover_batch_characters', return_value=copy.deepcopy(characters)), \
                 patch.object(benchmark_runner.generate_personas, '_compile_persona', side_effect=compile_function):
                result = benchmark_runner._run_persona_case(fixture, object(), 'model', 4096)
            self.assertEqual('passed' if passed else 'failed', result['status'])
            self.assertEqual(passed, result['quality']['passed'])
            self.assertEqual(1 if passed else 0, result['quality']['speaker_coverage'])
            self.assertEqual(1, result['quality']['evidence_coverage'])
            self.assertEqual(1, result['discovery_calls'])
            self.assertEqual(1, result['compile_calls'])
        self.assertEqual(original, fixture)



class RemoteTrainingCleanupTests(unittest.TestCase):
    def test_actual_staging_files_removed_after_success_transfer_worker_timeout_and_cancel(self):
        import copy
        import shlex
        import shutil
        import subprocess
        from types import SimpleNamespace
        from benchmark_execution import BenchmarkCancelled
        actual_run=subprocess.run
        created=[]
        for mode in ('success','transfer','worker','timeout','cancel'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);dataset=root/'dataset';dataset.mkdir()
                (dataset/'metadata.jsonl').write_text('synthetic metadata')
                fixture={'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}}
                original=copy.deepcopy(fixture);paths=[];cleanup=[]
                def stage(command,**kwargs):
                    if command[0]=='scp':
                        target=command[-1].split(':',1)[1]
                        shutil.copyfile(command[1],target)
                        return SimpleNamespace(returncode=int(mode=='transfer'),stderr='transfer refused')
                    args=shlex.split(command[2])
                    result=actual_run(args,capture_output=True,text=True,check=True)
                    if args[0]=='mktemp':
                        path=result.stdout.strip();created.append(path);paths.append(path)
                        result.stdout='SSH banner\n'+result.stdout
                    return result
                def remove(command,**kwargs):
                    args=shlex.split(command[2]);self.assertEqual(['rm','-rf','--'],args[:3])
                    self.assertEqual(paths[0],args[3]);self.assertEqual(30,kwargs['timeout'])
                    self.assertTrue(Path(args[3],'metadata.jsonl').exists())
                    cleanup.append(args[3]);shutil.rmtree(args[3])
                    return SimpleNamespace(returncode=0)
                failures={'transfer':RuntimeError,'worker':ValueError,'timeout':subprocess.TimeoutExpired,'cancel':BenchmarkCancelled}
                error={'worker':ValueError('worker refused'),'timeout':subprocess.TimeoutExpired('worker',7200),
                       'cancel':BenchmarkCancelled('cancelled')}.get(mode)
                worker_result={'status':'passed'}
                try:
                    with patch.object(benchmark_runner,'run_benchmark_subprocess',side_effect=stage), \
                         patch.object(benchmark_runner.subprocess,'run',side_effect=remove), \
                         patch.object(benchmark_runner,'run_benchmark_worker',side_effect=error,return_value=worker_result) as worker:
                        if mode=='success':
                            self.assertEqual(worker_result,benchmark_runner._run_lora_training_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},str(root),'fixture-host'))
                        else:
                            with self.assertRaises(failures[mode]) as caught:
                                benchmark_runner._run_lora_training_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},str(root),'fixture-host')
                            if error is not None:self.assertIs(error,caught.exception)
                        if mode=='transfer':worker.assert_not_called()
                    self.assertEqual(paths,cleanup)
                    self.assertFalse(Path(paths[0]).exists())
                    self.assertEqual(original,fixture)
                finally:
                    for path in paths:
                        if Path(path).exists():shutil.rmtree(path)
        self.assertEqual(len(created),len(set(created)))

    def test_cleanup_errors_do_not_replace_primary_failure_and_are_reported(self):
        import subprocess
        from types import SimpleNamespace
        errors=(OSError('SSH unavailable'),subprocess.TimeoutExpired('cleanup',30),None)
        for cleanup_error in errors:
            for primary in (False,True):
                with self.subTest(cleanup_error=cleanup_error,primary=primary):
                    original=ValueError('original worker failure')
                    stage=SimpleNamespace(returncode=0,stdout='/tmp/alexandria-lora-training.abcdefghij\n',stderr='')
                    with patch.object(benchmark_runner,'run_benchmark_subprocess',return_value=stage), \
                         patch.object(benchmark_runner,'run_benchmark_worker',side_effect=original if primary else None,return_value={}), \
                         patch.object(benchmark_runner.subprocess,'run',side_effect=cleanup_error,return_value=SimpleNamespace(returncode=1)), \
                         self.assertLogs('benchmark_runner',level='WARNING') as logs:
                        with self.assertRaises(ValueError if primary else RuntimeError) as caught:
                            benchmark_runner._run_lora_training_worker({'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}},'thunder',{'remote_root':'/remote','remote_python':'python3'},'/fixture','fixture-host')
                    if primary:self.assertIs(original,caught.exception)
                    self.assertIn('cleanup failed',logs.output[0])

    def test_local_training_and_unvalidated_remote_paths_are_never_deleted(self):
        from types import SimpleNamespace
        fixture={'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}}
        with patch.object(benchmark_runner,'run_benchmark_worker',return_value={'status':'passed'}), \
             patch.object(benchmark_runner.subprocess,'run') as cleanup:
            self.assertEqual({'status':'passed'},benchmark_runner._run_lora_training_worker(fixture,'local',{},'/fixture',None))
            cleanup.assert_not_called()
        for path in ('/tmp','/tmp/alexandria-lora-training.abcdefghij/other',''):
            with self.subTest(path=path), \
                 patch.object(benchmark_runner,'run_benchmark_subprocess',return_value=SimpleNamespace(returncode=0,stdout=path,stderr='')), \
                 patch.object(benchmark_runner.subprocess,'run') as cleanup:
                with self.assertRaises(ValueError):benchmark_runner._run_lora_training_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},'/fixture','fixture-host')
                cleanup.assert_not_called()
