import asyncio
import csv
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from fastapi import BackgroundTasks

import core
from routers import voicelab
from voicelab_settings import get_profiler_paths


ROOT = Path(__file__).resolve().parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch_train = load_script("batch_train_lora")
voice_profiler = load_script("voice_profiler")


class VoiceLabPipelineScriptTests(unittest.TestCase):
    def test_profiler_epub_search_uses_only_explicit_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = os.path.join(tmp, "Example Book [B012345678].epub")
            Path(expected).write_bytes(b"epub")

            self.assertEqual(expected, voice_profiler.find_epub(
                "narrator_test_voice_example_book_b012345678_char1_vol01", [tmp]))
            self.assertIsNone(voice_profiler.find_epub(
                "narrator_test_voice_example_book_b012345678_char1_vol01", []))

    def test_profiler_preflight_reports_missing_model_and_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "manifest.json")
            Path(manifest).write_text("[]", encoding="utf-8")
            with patch.object(voice_profiler, "DEPENDENCY_ERROR", ImportError("no librosa")), \
                 patch.dict(sys.modules, {"llama_cpp": None}):
                report = voice_profiler.get_preflight_report(
                    manifest, os.path.join(tmp, "missing.gguf"),
                    os.path.join(tmp, "profiles.csv"), [])

        self.assertEqual("failed", report["status"])
        self.assertTrue(any("acoustic dependency" in error for error in report["errors"]))
        self.assertTrue(any("model not found" in error for error in report["errors"]))

    def test_profiler_model_errors_are_classified(self):
        self.assertIn("insufficient GPU memory", voice_profiler.describe_model_init_error(
            RuntimeError("HIP out of memory")))
        self.assertIn("invalid or incompatible GGUF", voice_profiler.describe_model_init_error(
            ValueError("bad GGUF magic")))

    def test_profiler_model_init_failure_is_concise_and_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            model_path = os.path.join(tmp, "model.gguf")
            csv_path = os.path.join(tmp, "profiles.csv")
            original = '[{"id":"voice","zip_source":"voice.zip"}]'
            Path(manifest_path).write_text(original, encoding="utf-8")
            Path(model_path).write_bytes(b"model")
            fake_llama = SimpleNamespace(Llama=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("HIP out of memory")))
            argv = ["voice_profiler.py", "--manifest", manifest_path,
                    "--model", model_path, "--output_csv", csv_path]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), \
                 patch.dict(sys.modules, {"llama_cpp": fake_llama}), \
                 redirect_stdout(output):
                rc = voice_profiler.main()

            self.assertEqual(1, rc)
            self.assertIn("insufficient GPU memory", output.getvalue())
            self.assertEqual(original, Path(manifest_path).read_text(encoding="utf-8"))

    def test_profiler_defaults_are_checkout_and_data_root_relative(self):
        with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as data:
            paths = get_profiler_paths(checkout, data)

        self.assertEqual(os.path.join(checkout, "Qwen2.5-14B-Instruct-Q6_K.gguf"),
                         paths["model"])
        self.assertEqual(os.path.join(data, "lora_models", "manifest.json"),
                         paths["manifest"])
        self.assertEqual(os.path.join(data, "lora_models", "voice_profiles.csv"),
                         paths["output_csv"])

    def make_adapter(self, path, meta=None):
        os.makedirs(path)
        Path(path, "adapter_config.json").write_text("{}", encoding="utf-8")
        Path(path, "adapter_model.safetensors").write_bytes(b"weights")
        Path(path, "training_meta.json").write_text(
            json.dumps(meta or {"best_loss": 1.0}), encoding="utf-8")

    def test_only_complete_adapters_are_resume_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = os.path.join(tmp, "speaker_100")
            os.makedirs(partial)
            self.assertIsNone(batch_train.adapter_exists(tmp, "speaker", []))

            complete = os.path.join(tmp, "renamed_voice")
            self.make_adapter(complete)
            manifest = [{"id": "renamed_voice", "dataset_id": "speaker"}]
            self.assertEqual(complete, batch_train.adapter_exists(tmp, "speaker", manifest))

    def test_training_failure_removes_new_partial_output_and_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "speaker.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("metadata.jsonl", '{"audio": "missing.wav"}\n')
            args = SimpleNamespace(
                datasets_dir=os.path.join(tmp, "datasets"),
                models_dir=os.path.join(tmp, "models"),
                python=sys.executable,
                train_script="train_lora.py",
                max_epochs=1,
                lr=1e-6,
                lora_r=4,
                lora_alpha=8,
                grad_accum=1,
                language="english",
                target_loss=4.0,
                keep_datasets=False,
            )
            os.makedirs(args.datasets_dir)
            os.makedirs(args.models_dir)

            class FailedProcess:
                def __init__(self, command, **kwargs):
                    os.makedirs(command[command.index("--output_dir") + 1])
                    self.stdout = []
                    self.returncode = 2

                def wait(self):
                    return self.returncode

            with patch.object(batch_train.subprocess, "Popen", FailedProcess):
                result = batch_train.train_one(zpath, "speaker", "speaker_100", args)

            self.assertIsNone(result)
            self.assertFalse(os.path.exists(os.path.join(args.datasets_dir, "speaker")))
            self.assertFalse(os.path.exists(os.path.join(args.models_dir, "speaker_100")))

    def test_batch_returns_failure_after_processing_all_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            zips = os.path.join(tmp, "zips")
            os.makedirs(zips)
            for name in ("one.zip", "two.zip"):
                Path(zips, name).write_bytes(b"not used")
            argv = ["batch_train_lora.py", "--zips_dir", zips,
                    "--datasets_dir", os.path.join(tmp, "datasets"),
                    "--models_dir", os.path.join(tmp, "models"),
                    "--manifest", os.path.join(tmp, "models", "manifest.json")]
            with patch.object(sys, "argv", argv), \
                 patch.object(batch_train, "train_one", return_value=None) as train_one:
                rc = batch_train.main()

            self.assertEqual(1, rc)
            self.assertEqual(2, train_one.call_count)

    def test_successful_subprocess_with_incomplete_adapter_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "speaker.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("metadata.jsonl", '{"audio": "missing.wav"}\n')
            args = SimpleNamespace(
                datasets_dir=os.path.join(tmp, "datasets"),
                models_dir=os.path.join(tmp, "models"),
                python=sys.executable,
                train_script="train_lora.py",
                max_epochs=1, lr=1e-6, lora_r=4, lora_alpha=8, grad_accum=1,
                language="english", target_loss=4.0, keep_datasets=False,
            )
            os.makedirs(args.datasets_dir)
            os.makedirs(args.models_dir)

            class IncompleteProcess:
                def __init__(self, command, **kwargs):
                    os.makedirs(command[command.index("--output_dir") + 1])
                    self.stdout = []
                    self.returncode = 0

                def wait(self):
                    return self.returncode

            with patch.object(batch_train.subprocess, "Popen", IncompleteProcess):
                result = batch_train.train_one(zpath, "speaker", "speaker_100", args)

            self.assertIsNone(result)
            self.assertFalse(os.path.exists(os.path.join(args.models_dir, "speaker_100")))

    def test_missing_or_unreadable_reference_returns_failure(self):
        for failure in (None, RuntimeError("bad audio")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                manifest_path = os.path.join(tmp, "manifest.json")
                manifest = [{"id": "voice", "dataset_id": "narrator_test_voice_book",
                             "zip_source": "voice.zip"}]
                Path(manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
                argv = ["voice_profiler.py", "--manifest", manifest_path, "--dry_run"]
                analyze = patch.object(voice_profiler, "analyze_ref_wav",
                                       side_effect=failure if failure else None)
                with patch.object(sys, "argv", argv), \
                     patch.object(voice_profiler, "get_ref_wav",
                                  return_value=None if failure is None else b"wav"), analyze:
                    rc = voice_profiler.main()
                self.assertEqual(1, rc)

    def test_profiler_failure_stays_pending_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            model_path = os.path.join(tmp, "model.gguf")
            csv_path = os.path.join(tmp, "profiles.csv")
            Path(model_path).write_bytes(b"model")
            manifest = [{"id": "voice", "dataset_id": "narrator_test_voice_book",
                         "zip_source": "voice.zip"}]
            Path(manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
            features = {
                "mean_f0": 120.0, "std_f0": 10.0, "mean_rms": 0.04,
                "speaking_rate": 3.0, "mean_centroid": 2000.0,
                "mean_rolloff": 3000.0, "smoothness": 0.4,
                "flatness": 0.03, "duration": 5.0,
            }
            fake_llama = SimpleNamespace(Llama=lambda **kwargs: object())
            argv = ["voice_profiler.py", "--manifest", manifest_path,
                    "--model", model_path, "--output_csv", csv_path]
            with patch.object(sys, "argv", argv), \
                 patch.dict(sys.modules, {"llama_cpp": fake_llama}), \
                 patch.object(voice_profiler, "get_ref_wav", return_value=b"wav"), \
                 patch.object(voice_profiler, "analyze_ref_wav", return_value=features), \
                 patch.object(voice_profiler, "find_epub", return_value=None), \
                 patch.object(voice_profiler, "llm_describe", side_effect=RuntimeError("offline")):
                rc = voice_profiler.main()

            saved = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(1, rc)
            self.assertNotIn("voice_profile", saved[0])
            self.assertIn("voice_features", saved[0])

    def test_resumed_profiling_csv_contains_existing_and_new_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            model_path = os.path.join(tmp, "model.gguf")
            csv_path = os.path.join(tmp, "profiles.csv")
            Path(model_path).write_bytes(b"model")
            features = {
                "mean_f0": 120.0, "std_f0": 10.0, "mean_rms": 0.04,
                "speaking_rate": 3.0, "mean_centroid": 2000.0,
                "mean_rolloff": 3000.0, "smoothness": 0.4,
                "flatness": 0.03, "duration": 5.0,
            }
            existing_features = {key: features[key] for key in
                                 ("mean_f0", "std_f0", "mean_rms", "speaking_rate",
                                  "mean_centroid", "smoothness", "flatness")}
            manifest = [
                {"id": "old", "dataset_id": "narrator_old_voice_book",
                 "zip_source": "old.zip", "voice_profile": "existing profile",
                 "voice_features": existing_features},
                {"id": "new", "dataset_id": "narrator_new_voice_book",
                 "zip_source": "new.zip"},
            ]
            Path(manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
            fake_llama = SimpleNamespace(Llama=lambda **kwargs: object())
            argv = ["voice_profiler.py", "--manifest", manifest_path,
                    "--model", model_path, "--output_csv", csv_path]
            with patch.object(sys, "argv", argv), \
                 patch.dict(sys.modules, {"llama_cpp": fake_llama}), \
                 patch.object(voice_profiler, "get_ref_wav", return_value=b"wav"), \
                 patch.object(voice_profiler, "analyze_ref_wav", return_value=features), \
                 patch.object(voice_profiler, "find_epub", return_value=None), \
                 patch.object(voice_profiler, "llm_describe", return_value="new profile"):
                rc = voice_profiler.main()

            with open(csv_path, newline="", encoding="utf-8") as f:
                ids = [row["id"] for row in csv.DictReader(f)]
            self.assertEqual(0, rc)
            self.assertEqual(["old", "new"], ids)

    def test_atomic_csv_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "profiles.csv")
            Path(target).write_text("old content", encoding="utf-8")
            with patch.object(voice_profiler.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    voice_profiler.atomic_csv_write([], target)
            self.assertEqual("old content", Path(target).read_text(encoding="utf-8"))

    def test_train_and_profile_background_dispatch_reaches_subprocess(self):
        for stage in ("train", "profile"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "_deduped"))
                cfg = {"rocm_python": sys.executable, "profiler_model": "",
                       "epub_dirs": [], "zips_dir": tmp}
                background = BackgroundTasks()
                streamed = []

                def fake_stream(command, cwd, state, **kwargs):
                    streamed.append(command)
                    return 0, []

                request = voicelab.VoiceLabRequest(stages=[stage])
                with patch.object(voicelab, "check_global_gpu_lock"), \
                     patch.object(voicelab, "claim_gpu_task"), \
                     patch.object(voicelab, "_load_voicelab_config", return_value=cfg), \
                     patch.object(voicelab, "_validate_voicelab_path"), \
                     patch.object(voicelab, "_revalidate_voicelab_paths", return_value=None), \
                     patch.object(voicelab, "_run_profiler_preflight", return_value={}), \
                     patch.object(voicelab, "_init_task_log", return_value=None), \
                     patch.object(voicelab, "_stream_subprocess_to_logs", side_effect=fake_stream):
                    asyncio.run(voicelab.voicelab_start(request, background))
                    task = background.tasks[0]
                    task.func(*task.args, **task.kwargs)

                self.assertEqual(1, len(streamed))
                self.assertIn(stage, os.path.basename(streamed[0][2]))
                if stage == "profile":
                    defaults = get_profiler_paths(core.ROOT_DIR, core.DATA_DIR)
                    self.assertEqual(defaults["manifest"],
                                     streamed[0][streamed[0].index("--manifest") + 1])
                    self.assertEqual(defaults["model"],
                                     streamed[0][streamed[0].index("--model") + 1])
                    self.assertEqual(defaults["output_csv"],
                                     streamed[0][streamed[0].index("--output_csv") + 1])


if __name__ == "__main__":
    unittest.main()
