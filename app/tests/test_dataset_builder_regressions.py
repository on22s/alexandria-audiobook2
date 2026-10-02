"""Dataset edits and generation failures must preserve valid row state."""

import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from routers import dataset_builder


class DatasetBuilderRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = {"text": "A sample line.", "emotion": "calm", "seed": "0",
                         "status": "done", "audio_url": "/old.wav", "description": "warm"}
        self.state = {"description": "warm", "global_seed": "0",
                      "samples": [copy.deepcopy(self.original)]}
        for attr, value in (("DATASET_BUILDER_DIR", str(self.root)),
                            ("process_state", {"dataset_builder": {"running": False}})):
            patcher = patch.object(dataset_builder, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dataset_builder._save_builder_state("voice", self.state)

    def test_http_row_save_accepts_integer_and_legacy_string_seeds_without_coercion(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(dataset_builder.router)
        with TestClient(app) as client:
            for seed in (0, 42, -1, 2147483647, "0", "42", ""):
                with self.subTest(seed=seed):
                    state = copy.deepcopy(self.state)
                    state["samples"][0]["seed"] = seed
                    dataset_builder._save_builder_state("voice", state)
                    row = {key: state["samples"][0][key] for key in ("text", "emotion", "seed")}
                    request = {"name": "voice", "rows": [row]}
                    before = copy.deepcopy(request)
                    response = client.post("/api/dataset_builder/update_rows", json=request)
                    self.assertEqual(200, response.status_code, response.text)
                    saved = dataset_builder._load_builder_state("voice")
                    sample = saved["samples"][0]
                    self.assertEqual({"status": "ok", "sample_count": 1,
                                      "row_revisions": [dataset_builder.get_dataset_row_revision(sample)]}, response.json())
                    self.assertEqual(seed, sample["seed"])
                    self.assertIs(type(seed), type(sample["seed"]))
                    self.assertEqual("done", sample["status"])
                    self.assertEqual("/old.wav", sample["audio_url"])
                    self.assertEqual("warm", saved["description"])
                    self.assertEqual("0", saved["global_seed"])
                    self.assertEqual(before, request)

    def test_http_invalid_row_seed_types_refuse_entire_write_and_keep_batch_guard(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(dataset_builder.router)
        state_path = self.root / "voice/state.json"
        original = state_path.read_bytes()
        with TestClient(app) as client:
            for seed in (True, False, None, 1.5, [], {}):
                with self.subTest(seed=seed):
                    response = client.post("/api/dataset_builder/update_rows", json={
                        "name": "voice", "rows": [{"text": "first", "seed": 0},
                        {"text": "invalid second", "seed": seed}]})
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertIn("seed", response.json()["detail"])
                    self.assertEqual(original, state_path.read_bytes())
            dataset_builder.process_state["dataset_builder"]["running"] = True
            response = client.post("/api/dataset_builder/update_rows", json={
                "name": "voice", "rows": [{"text": "blocked", "seed": 0}]})
            self.assertEqual(409, response.status_code)
            self.assertEqual(original, state_path.read_bytes())

    def test_delete_rejects_root_equivalent_traversal_and_escaping_symlink_paths(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(dataset_builder.router)
        sentinel = self.root / "root_sentinel.txt"
        sentinel.write_bytes(b"root must survive")
        state_path = self.root / "voice/state.json"
        original = state_path.read_bytes()
        with tempfile.TemporaryDirectory() as outside:
            outside_root = Path(outside)
            outside_sentinel = outside_root / "outside_sentinel.txt"
            outside_sentinel.write_bytes(b"outside must survive")
            (self.root / "escape_link").symlink_to(outside_root, target_is_directory=True)
            (self.root / "root_link").symlink_to(self.root, target_is_directory=True)
            with patch.object(dataset_builder.shutil, "rmtree") as remove:
                for name in ("", ".", "..", "voice/..", str(self.root), str(outside_root),
                             "escape_link", "root_link"):
                    with self.subTest(name=name), self.assertRaises(HTTPException) as raised:
                        asyncio.run(dataset_builder.dataset_builder_delete(name))
                    self.assertEqual(400, raised.exception.status_code)
                with TestClient(app) as client:
                    for path in ("%2e", "%2e%2e", "voice%2f..", "escape_link", "root_link"):
                        response = client.delete("/api/dataset_builder/" + path)
                        self.assertIn(response.status_code, (400, 404), response.text)
                remove.assert_not_called()
            self.assertEqual(b"outside must survive", outside_sentinel.read_bytes())
        self.assertEqual(b"root must survive", sentinel.read_bytes())
        self.assertEqual(original, state_path.read_bytes())

    def test_http_delete_removes_only_selected_project_and_preserves_root_and_other_project(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(dataset_builder.router)
        other = self.root / "another"
        other.mkdir()
        sentinel = other / "keep.txt"
        sentinel.write_bytes(b"other project")
        with TestClient(app) as client:
            response = client.delete("/api/dataset_builder/voice")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"status": "deleted", "name": "voice"}, response.json())
        self.assertFalse((self.root / "voice").exists())
        self.assertTrue(self.root.is_dir())
        self.assertEqual(b"other project", sentinel.read_bytes())

    def test_changed_emotion_or_seed_invalidates_completed_audio(self):
        for key, value in (("emotion", "angry"), ("seed", "9")):
            with self.subTest(key=key):
                dataset_builder._save_builder_state("voice", self.state)
                row = {key: self.original[key] for key in ("text", "emotion", "seed")}
                row[key] = value
                asyncio.run(dataset_builder.dataset_builder_update_rows(
                    dataset_builder.DatasetBuilderUpdateRowsRequest(name="voice", rows=[row])))
                updated = dataset_builder._load_builder_state("voice")["samples"][0]
                self.assertEqual("pending", updated["status"])
                self.assertIsNone(updated["audio_url"])
                self.assertEqual(value, updated[key])

    def test_unchanged_inputs_preserve_completed_audio(self):
        row = {key: self.original[key] for key in ("text", "emotion", "seed")}
        row["text"] = "  " + row["text"] + "  "
        asyncio.run(dataset_builder.dataset_builder_update_rows(
            dataset_builder.DatasetBuilderUpdateRowsRequest(name="voice", rows=[row])))
        updated = dataset_builder._load_builder_state("voice")["samples"][0]
        self.assertEqual("done", updated["status"])
        self.assertEqual("/old.wav", updated["audio_url"])

    def test_single_sample_requires_a_saved_project_and_an_existing_row_before_gpu_claim(self):
        for name, index, expected in (("missing", 0, 404), ("empty_directory", 0, 404), ("voice", 1, 400)):
            with self.subTest(name=name):
                if name == "empty_directory":
                    (self.root / name).mkdir()
                manager = SimpleNamespace(get_engine=Mock(side_effect=RuntimeError("must not initialize")))
                request = dataset_builder.DatasetSampleGenRequest(
                    dataset_name=name, sample_index=index, description="warm", text="Hello", seed=1)
                before = (self.root / "voice/state.json").read_bytes()
                with patch.object(dataset_builder, "project_manager", manager), \
                     patch.object(dataset_builder, "check_global_gpu_lock"), \
                     patch.object(dataset_builder, "claim_gpu_task") as claim, \
                     patch.object(dataset_builder.logger, "exception"):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(dataset_builder.dataset_builder_generate_sample(request))
                self.assertEqual(expected, raised.exception.status_code)
                manager.get_engine.assert_not_called()
                claim.assert_not_called()
                self.assertEqual(before, (self.root / "voice/state.json").read_bytes())
                self.assertFalse((self.root / "missing").exists())
                self.assertFalse((self.root / "empty_directory/state.json").exists())

    def test_existing_row_single_sample_publishes_audio_and_preserves_row_metadata(self):
        import numpy as np
        import soundfile as sf
        from audio_validation import validate_generated_audio
        wav = self.root / "generated.wav"
        sf.write(wav, np.zeros(1200), 24000)
        engine = SimpleNamespace(generate_voice_design=Mock(return_value=(str(wav), None)))
        manager = SimpleNamespace(get_engine=lambda: engine)
        request = dataset_builder.DatasetSampleGenRequest(
            dataset_name="voice", sample_index=0, description="new voice", text="New text", seed=9)
        with patch.object(dataset_builder, "project_manager", manager), \
             patch.object(dataset_builder, "check_global_gpu_lock"), \
             patch.object(dataset_builder, "claim_gpu_task") as claim:
            response = asyncio.run(dataset_builder.dataset_builder_generate_sample(request))
        self.assertEqual("done", response["status"])
        claim.assert_called_once_with("dataset_builder")
        updated = dataset_builder._load_builder_state("voice")["samples"]
        self.assertEqual(1, len(updated))
        self.assertEqual("New text", updated[0]["text"])
        self.assertEqual("calm", updated[0]["emotion"])
        output = str(self.root / "voice/sample_000.wav")
        self.assertEqual(output, validate_generated_audio(output))
        self.assertEqual(wav.read_bytes(), Path(output).read_bytes())
        self.assertFalse(dataset_builder.process_state["dataset_builder"]["running"])

    def test_generation_failure_preserves_row_and_prior_audio(self):
        old_audio = self.root / "voice/sample_000.wav"
        old_audio.write_bytes(b"prior audio")
        engine = SimpleNamespace(generate_voice_design=Mock(side_effect=RuntimeError("generation failed")))
        manager = SimpleNamespace(get_engine=lambda: engine)
        request = dataset_builder.DatasetSampleGenRequest(
            dataset_name="voice", sample_index=0, description="new voice", text="New text", seed=9)
        with patch.object(dataset_builder, "project_manager", manager), \
             patch.object(dataset_builder, "check_global_gpu_lock"), \
             patch.object(dataset_builder, "claim_gpu_task") as claim, \
             patch.object(dataset_builder.logger, "exception"):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(dataset_builder.dataset_builder_generate_sample(request))
        self.assertEqual(500, raised.exception.status_code)
        claim.assert_called_once_with("dataset_builder")
        updated = dataset_builder._load_builder_state("voice")["samples"][0]
        for key, value in self.original.items():
            if key != "status":
                self.assertEqual(value, updated.get(key), key)
        self.assertEqual("error", updated["status"])
        self.assertEqual("generation failed", updated["error"])
        self.assertEqual(b"prior audio", old_audio.read_bytes())
        self.assertFalse(dataset_builder.process_state["dataset_builder"]["running"])


if __name__ == "__main__":
    unittest.main()


class DatasetReferenceIndexTests(unittest.TestCase):
    def _prepare(self, root):
        import wave
        builder = root / 'builder'
        work = builder / 'voice'
        work.mkdir(parents=True)
        state = {'samples': [{'status': 'done', 'text': 'First reference'},
                             {'status': 'done', 'text': 'Second reference'}]}
        for index in (0, 1):
            with wave.open(str(work / ('sample_%03d.wav' % index)), 'wb') as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(bytes([index + 1, 0]) * 240)
        import json
        (work / 'state.json').write_text(json.dumps(state))
        return builder, work, root / 'datasets'

    def test_negative_reference_is_rejected_before_loading_or_publishing(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(dataset_builder.router)
        for index in (-1, -42):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                builder, work, output = self._prepare(Path(tmp))
                original = {p.name: p.read_bytes() for p in work.iterdir()}
                with patch.object(dataset_builder, 'DATASET_BUILDER_DIR', str(builder)), \
                     patch.object(dataset_builder, 'LORA_DATASETS_DIR', str(output)), \
                     patch.object(dataset_builder, 'process_state', {'dataset_builder': {'running': False}}), \
                     patch.object(dataset_builder, '_load_builder_state', wraps=dataset_builder._load_builder_state) as load:
                    response = client.post('/api/dataset_builder/save', json={'name': 'voice', 'ref_index': index})
                self.assertEqual(422, response.status_code)
                load.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(original, {p.name: p.read_bytes() for p in work.iterdir()})

    def test_zero_later_and_omitted_choices_preserve_real_reference_audio_and_text(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(dataset_builder.router)
        for choice in (0, 1, None, 'omitted'):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                builder, work, output = self._prepare(Path(tmp))
                original = {p.name: p.read_bytes() for p in work.iterdir()}
                body = {'name': 'voice'}
                if choice != 'omitted':
                    body['ref_index'] = choice
                with patch.object(dataset_builder, 'DATASET_BUILDER_DIR', str(builder)), \
                     patch.object(dataset_builder, 'LORA_DATASETS_DIR', str(output)), \
                     patch.object(dataset_builder, 'process_state', {'dataset_builder': {'running': False}}):
                    response = client.post('/api/dataset_builder/save', json=body)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(2, response.json()['sample_count'])
                index = 1 if choice == 1 else 0
                saved = output / 'voice'
                self.assertEqual(original['sample_%03d.wav' % index], (saved / 'ref.wav').read_bytes())
                self.assertEqual('Second reference' if index else 'First reference',
                                 (saved / 'ref_text.txt').read_text())
                self.assertEqual(original, {p.name: p.read_bytes() for p in work.iterdir()})


class GeneratedSampleSeedTests(unittest.TestCase):
    setUp = DatasetBuilderRegressionTests.setUp

    def test_http_regeneration_persists_used_seed_and_preserves_row_fields(self):
        import wave
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        source = self.root / "render.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(bytes((0, 16)) * 1200)
        original = source.read_bytes()
        engine = SimpleNamespace(generate_voice_design=Mock(return_value=(str(source), None)))
        app = FastAPI()
        app.include_router(dataset_builder.router)
        with TestClient(app) as client, \
             patch.object(dataset_builder, "check_global_gpu_lock"), \
             patch.object(dataset_builder, "claim_gpu_task"), \
             patch.object(dataset_builder.project_manager, "get_engine", return_value=engine):
            for seed in (123, 0, -1):
                with self.subTest(seed=seed):
                    request = {"dataset_name": "voice", "sample_index": 0, "description": "warm, calm", "text": " A new line. ", "seed": seed}
                    before = copy.deepcopy(request)
                    response = client.post("/api/dataset_builder/generate_sample", json=request)
                    self.assertEqual(200, response.status_code, response.text)
                    row = dataset_builder._load_builder_state("voice")["samples"][0]
                    self.assertEqual(seed, row["seed"])
                    self.assertEqual(seed, engine.generate_voice_design.call_args.kwargs["seed"])
                    self.assertEqual("calm", row["emotion"])
                    self.assertEqual("done", row["status"])
                    self.assertEqual("A new line.", row["text"])
                    self.assertEqual(original, (self.root / "voice/sample_000.wav").read_bytes())
                    self.assertEqual(original, source.read_bytes())
                    self.assertFalse(dataset_builder.process_state["dataset_builder"]["running"])
                    self.assertEqual(before, request)


class DatasetBenchmarkThreadTests(unittest.TestCase):
    def test_actual_route_thread_finishes_after_benchmark_event_loop_is_closed(self):
        import array
        import asyncio
        import core
        import dataset_builder_benchmark as benchmark
        import threading
        import wave
        from tts_benchmark import measure_wav
        payload = {"fixture": {"description": "warm voice", "samples": [
            {"text": "First sample.", "emotion": "calm"},
            {"text": "Second sample.", "emotion": ""},
            {"text": "Third sample.", "emotion": "happy"}],
            "global_seed": 42, "seeds": [7, -1, 0]}, "tts": {"device": "cpu"}}
        before = copy.deepcopy(payload)
        state = {"dataset_builder": {"running": False, "logs": [], "cancel": False}}
        loop_closed = threading.Event()
        threads = []
        calls = []
        real_run, real_thread = asyncio.run, threading.Thread
        def run(coro):
            response = real_run(coro)
            loop_closed.set()
            return response
        def start_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            threads.append(worker)
            return worker
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "source.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(array.array('h', [4096] * 2400).tobytes())
            original = wav.read_bytes()
            expected = measure_wav(str(wav), 1)
            def render(**kwargs):
                if not loop_closed.wait(2):
                    raise AssertionError("benchmark loop did not close")
                self.assertTrue(state["dataset_builder"]["running"])
                calls.append(kwargs)
                return str(wav), kwargs["sample_text"]
            engine = SimpleNamespace(generate_voice_design=render)
            original_dir = dataset_builder.DATASET_BUILDER_DIR
            original_engine = dataset_builder.project_manager.get_engine
            with patch.object(core, "process_state", state), \
                 patch.object(core, "_gpu_leases", {}), \
                 patch.object(core, "acquire_gpu_lock", return_value=None), \
                 patch.object(dataset_builder, "process_state", state), \
                 patch.object(benchmark, "TTSEngine", return_value=engine), \
                 patch.object(benchmark.asyncio, "run", side_effect=run), \
                 patch.object(dataset_builder.threading, "Thread", side_effect=start_thread):
                try:
                    result = benchmark.execute_payload(payload)
                finally:
                    loop_closed.set()
                    for worker in threads:
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(threads))
            self.assertEqual("passed", result["status"])
            self.assertEqual(3, result["completed"])
            self.assertFalse(state["dataset_builder"]["running"])
            self.assertEqual("benchmark", state["dataset_builder"]["dataset_name"])
            self.assertTrue(any(line == "[DONE] Generated 3/3 samples" for line in result["logs"]))
            self.assertEqual([7, 42, 0], [call["seed"] for call in calls])
            self.assertEqual(["warm voice, calm", "warm voice", "warm voice, happy"],
                             [call["description"] for call in calls])
            for index, output in enumerate(result["outputs"]):
                self.assertEqual(index, output["index"])
                self.assertEqual("done", output["state"]["status"])
                self.assertEqual(expected["sha256"], output["metrics"]["sha256"])
                self.assertEqual(0.1, output["metrics"]["duration_seconds"])
            self.assertEqual(original, wav.read_bytes())
            self.assertEqual(original_dir, dataset_builder.DATASET_BUILDER_DIR)
            self.assertEqual(original_engine, dataset_builder.project_manager.get_engine)
            self.assertEqual(before, payload)


class DatasetAudioRevisionTests(unittest.TestCase):
    setUp = DatasetBuilderRegressionTests.setUp

    def test_rapid_single_and_batch_replacements_publish_new_urls_and_pcm(self):
        import array
        import threading
        import wave
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from urllib.parse import urlsplit
        app = FastAPI()
        app.include_router(dataset_builder.router)
        wav = self.root / "source.wav"
        calls, threads = [], []
        real_thread = threading.Thread
        def render(**kwargs):
            calls.append(kwargs)
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(array.array('h', [1024 * len(calls)] * 1200).tobytes())
            return str(wav), kwargs["sample_text"]
        def start_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            threads.append(worker)
            return worker
        engine = SimpleNamespace(generate_voice_design=render)
        dataset_builder.process_state["dataset_builder"]["cancel"] = False
        urls = []
        with TestClient(app) as client, \
             patch.object(dataset_builder, "check_global_gpu_lock"), \
             patch.object(dataset_builder, "claim_gpu_task"), \
             patch.object(dataset_builder.project_manager, "get_engine", return_value=engine), \
             patch.object(dataset_builder.time, "time", return_value=1700000000.125), \
             patch.object(dataset_builder.threading, "Thread", side_effect=start_thread):
            for index, mode in enumerate(("single", "single", "batch", "batch"), 1):
                if mode == "single":
                    response = client.post("/api/dataset_builder/generate_sample", json={
                        "dataset_name": "voice", "sample_index": 0, "description": "warm",
                        "text": "Current line.", "seed": 3})
                else:
                    response = client.post("/api/dataset_builder/generate_batch", json={
                        "name": "voice", "description": "warm", "global_seed": 3,
                        "samples": [{"text": "Current line.", "emotion": "calm"}]})
                    threads[-1].join(2)
                    self.assertFalse(threads[-1].is_alive())
                self.assertEqual(200, response.status_code, response.text)
                row = dataset_builder._load_builder_state("voice")["samples"][0]
                urls.append(row["audio_url"])
                self.assertEqual("/dataset_builder/voice/sample_000.wav", urlsplit(urls[-1]).path)
                self.assertEqual("done", row["status"])
                with wave.open(str(self.root / "voice/sample_000.wav"), "rb") as audio:
                    self.assertEqual(24000, audio.getframerate())
                    self.assertEqual(1200, audio.getnframes())
                    self.assertEqual(array.array('h', [1024 * index] * 1200).tobytes(), audio.readframes(1200))
                self.assertFalse(dataset_builder.process_state["dataset_builder"]["running"])
        self.assertEqual(4, len(set(urls)), urls)
        self.assertEqual([3] * 4, [call["seed"] for call in calls])
