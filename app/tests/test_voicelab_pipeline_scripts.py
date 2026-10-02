import asyncio
import copy
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
import archive_utils
from routers import voicelab
from voicelab_settings import get_deduped_zip_name, get_profiler_paths, get_voice_lab_script_path


ROOT = Path(__file__).resolve().parent.parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / "tools" / "voice_lab" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch_train = load_script("batch_train_lora")
voice_profiler = load_script("voice_profiler")


class VoiceLabPipelineScriptTests(unittest.TestCase):
    def test_batch_empty_normalization_has_stable_distinct_child_ids(self):
        ids = [batch_train.sanitize(name) for name in ("---.zip", "声.zip")]
        self.assertTrue(all(ids))
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(ids[0], batch_train.sanitize("---.zip"))
        self.assertEqual("valid_voice", batch_train.sanitize("Valid Voice.zip"))

    def test_batch_bad_zip_cannot_clean_the_datasets_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            zips = Path(tmp, "zips")
            datasets = Path(tmp, "datasets")
            zips.mkdir()
            datasets.mkdir()
            sentinel = datasets / "existing_dataset.txt"
            sentinel.write_text("keep")
            (zips / "---.zip").write_bytes(b"not a ZIP")
            argv = ["batch_train_lora.py", "--zips_dir", str(zips),
                    "--datasets_dir", str(datasets), "--models_dir", str(Path(tmp, "models")),
                    "--manifest", str(Path(tmp, "models", "manifest.json"))]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                rc = batch_train.main()
            self.assertEqual(1, rc)
            self.assertTrue(sentinel.exists(), "failed extraction removed the datasets root")
            self.assertEqual("keep", sentinel.read_text())
            self.assertEqual([sentinel], list(datasets.iterdir()))

    def test_batch_manifest_keeps_entry_written_during_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            zips = Path(tmp, "zips")
            zips.mkdir()
            Path(zips, "new.zip").write_bytes(b"unused")
            models = Path(tmp, "models")
            manifest = models / "manifest.json"
            argv = ["batch_train_lora.py", "--zips_dir", str(zips),
                    "--datasets_dir", str(Path(tmp, "datasets")),
                    "--models_dir", str(models), "--manifest", str(manifest)]

            def train_with_intervening_write(*args):
                batch_train.save_manifest(str(manifest), [{"id": "other", "dataset_id": "other"}])
                return {"id": "new", "dataset_id": "new"}

            with patch.object(sys, "argv", argv), \
                 patch.object(batch_train, "train_one", side_effect=train_with_intervening_write), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(0, batch_train.main())

            self.assertEqual({"other", "new"},
                             {entry["id"] for entry in batch_train.load_manifest(str(manifest))})

    def test_stage_scripts_resolve_within_the_selected_checkout(self):
        for name in ("audit_voice_datasets.py", "voice_analysis.py", "batch_train_lora.py",
                     "evaluate_lora.py", "voice_profiler.py", "name_voices.py"):
            path = get_voice_lab_script_path(str(ROOT), name)
            self.assertEqual(str(ROOT / "tools" / "voice_lab" / name), path)
            self.assertTrue(os.path.isfile(path))
        with self.assertRaises(ValueError):
            get_voice_lab_script_path(str(ROOT), "../unrelated.py")

    def test_deduped_zip_names_preserve_same_basename_from_different_narrators(self):
        first = get_deduped_zip_name("Narrator One", "volume_1.zip")
        second = get_deduped_zip_name("Narrator Two", "volume_1.zip")
        self.assertNotEqual(first, second)
        self.assertEqual(first, get_deduped_zip_name("Narrator One", "volume_1.zip"))
        self.assertTrue(first.endswith(".zip"))

    def test_batch_rejects_colliding_normalized_dataset_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            zips = os.path.join(tmp, "zips")
            os.makedirs(zips)
            for name in ("A-B.zip", "A B.zip"):
                Path(zips, name).write_bytes(b"not extracted")
            argv = ["batch_train_lora.py", "--zips_dir", zips,
                    "--datasets_dir", os.path.join(tmp, "datasets"),
                    "--models_dir", os.path.join(tmp, "models"),
                    "--manifest", os.path.join(tmp, "models", "manifest.json")]
            with patch.object(sys, "argv", argv), \
                 patch.object(batch_train, "train_one") as train_one:
                rc = batch_train.main()

            self.assertEqual(1, rc)
            train_one.assert_not_called()

    def test_batch_zip_validation_rejects_unsafe_and_oversized_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsafe = SimpleNamespace(filename="../../escape", file_size=1)
            archive = SimpleNamespace(infolist=lambda: [unsafe])
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                batch_train.validate_zip_members(archive, tmp)

            with patch.object(archive_utils, "MAX_ARCHIVE_MEMBERS", 0), \
                 self.assertRaisesRegex(ValueError, "more than"):
                batch_train.validate_zip_members(archive, tmp)

            oversized = SimpleNamespace(filename="large.wav",
                                        file_size=archive_utils.MAX_ARCHIVE_BYTES + 1)
            archive = SimpleNamespace(infolist=lambda: [oversized])
            with self.assertRaisesRegex(ValueError, "20 GB"):
                batch_train.validate_zip_members(archive, tmp)

            normal = SimpleNamespace(filename="audio.wav", file_size=10)
            archive = SimpleNamespace(infolist=lambda: [normal])
            disk = SimpleNamespace(free=5)
            with patch.object(archive_utils.shutil, "disk_usage", return_value=disk), \
                 self.assertRaisesRegex(ValueError, "available extraction disk"):
                batch_train.validate_zip_members(archive, tmp)

    def test_batch_defaults_follow_loaded_checkout(self):
        self.assertEqual(str(ROOT), batch_train.REPO2_DIR)
        self.assertEqual(str(ROOT / "app" / "train_lora.py"), batch_train.TRAIN_SCRIPT)
        self.assertEqual(str(ROOT / "lora_models"), batch_train.MODELS_DIR)

    def test_profiler_epub_search_uses_only_explicit_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = os.path.join(tmp, "Example Book [B012345678].EPUB")
            Path(expected).write_bytes(b"epub")

            self.assertEqual(expected, voice_profiler.find_epub(
                "narrator_test_voice_example_book_b012345678_char1_vol01", [tmp]))
            self.assertIsNone(voice_profiler.find_epub(
                "narrator_test_voice_example_book_b012345678_char1_vol01", []))

    def test_profiler_identity_parser_keeps_middle_initial_and_book_boundary(self):
        dataset_id = "narrator_jane_q_public_example_book_b012345678_char1_vol01"
        self.assertEqual(("Jane Q Public", "Example Book", "B012345678"),
                         voice_profiler.get_dataset_identity(dataset_id))

    def test_completed_profiles_rebuild_csv_without_loading_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            csv_path = os.path.join(tmp, "profiles.csv")
            manifest = [{
                "id": "voice", "dataset_id": "narrator_jane_doe_example_book",
                "zip_source": "voice.zip", "voice_profile": "warm",
                "voice_features": {"mean_f0": 120, "std_f0": 10,
                                   "speaking_rate": 3},
            }]
            Path(manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
            argv = ["voice_profiler.py", "--manifest", manifest_path,
                    "--model", os.path.join(tmp, "missing.gguf"),
                    "--output_csv", csv_path]
            with patch.object(sys, "argv", argv):
                rc = voice_profiler.main()

            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(0, rc)
            self.assertEqual(["voice"], [row["id"] for row in rows])

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
        from tests.test_support import write_test_adapter
        write_test_adapter(path)
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
                    self.stdout = io.StringIO("")
                    self.returncode = 2

                def wait(self):
                    return self.returncode

                def poll(self):
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
                    self.stdout = io.StringIO("")
                    self.returncode = 0

                def wait(self):
                    return self.returncode

                def poll(self):
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

    def test_quality_train_evaluate_and_profile_background_dispatch_reaches_subprocess(self):
        for stage in ("quality", "train", "evaluate", "profile"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "_deduped"))
                cfg = {"rocm_python": sys.executable, "profiler_model": "",
                       "epub_dirs": [], "zips_dir": tmp}
                background = BackgroundTasks()
                streamed = []
                persisted = []

                def fake_stream(command, cwd, state, **kwargs):
                    streamed.append(command)
                    return 0, []

                request = voicelab.VoiceLabRequest(stages=[stage], preflight_id="test-ready")
                with patch.object(voicelab, "check_global_gpu_lock"), \
                     patch.object(voicelab, "claim_gpu_task"), \
                     patch.object(voicelab, "_load_voicelab_config", return_value=cfg), \
                     patch.object(voicelab, "_validate_voicelab_path"), \
                     patch.object(voicelab, "_build_voicelab_preflight",
                                  return_value={"preflight_id": "test-ready", "blockers": [],
                                                "_zips_dir": tmp, "_profiler_model": ""}), \
                     patch.object(voicelab, "_revalidate_voicelab_runtime",
                                  return_value=None) as revalidate, \
                     patch.object(voicelab, "_run_profiler_preflight", return_value={}), \
                     patch.object(voicelab, "_init_task_log", return_value=None), \
                     patch.object(voicelab, "_persist_voicelab_run",
                                  side_effect=lambda run_id, update:
                                  persisted.append((run_id, copy.deepcopy(update)))), \
                     patch.object(voicelab, "_stream_subprocess_to_logs", side_effect=fake_stream):
                    asyncio.run(voicelab.voicelab_start(request, background))
                    task = background.tasks[0]
                    task.func(*task.args, **task.kwargs)

                self.assertEqual(1, len(streamed))
                persisted_stage_states = [
                    update["stages"][0]["status"]
                    for _run_id, update in persisted
                    if update.get("stages")]
                self.assertIn("running", persisted_stage_states)
                self.assertIn("completed", persisted_stage_states)
                self.assertEqual([stage], revalidate.call_args.args[3])
                expected_script_token = "audit_voice_datasets" if stage == "quality" else stage
                self.assertIn(expected_script_token, os.path.basename(streamed[0][2]))
                if stage == "profile":
                    defaults = get_profiler_paths(core.ROOT_DIR, core.DATA_DIR)
                    self.assertEqual(defaults["manifest"],
                                     streamed[0][streamed[0].index("--manifest") + 1])
                    self.assertEqual(defaults["model"],
                                     streamed[0][streamed[0].index("--model") + 1])
                    self.assertEqual(defaults["output_csv"],
                                     streamed[0][streamed[0].index("--output_csv") + 1])
                if stage == "evaluate":
                    self.assertEqual(core.LORA_MODELS_MANIFEST,
                                     streamed[0][streamed[0].index("--manifest") + 1])
                    self.assertEqual(core.CONFIG_PATH,
                                     streamed[0][streamed[0].index("--config") + 1])
                if stage == "quality":
                    self.assertEqual(tmp, streamed[0][streamed[0].index("--zips2") + 1])
                if stage == "train":
                    self.assertEqual("2", streamed[0][streamed[0].index(
                        "--candidate_checkpoints") + 1])


if __name__ == "__main__":
    unittest.main()


class ProfilerEmptyResponseTests(unittest.TestCase):
    def test_empty_provider_responses_leave_profile_pending_and_fail_actual_cli(self):
        for content in ("", "  \n", '\"\"', "''", '\"  \"', "'  '", None):
            with self.subTest(content=content):
                self.run_profile(content, expected_failure=True)

    def test_real_describer_cleans_valid_content_and_cli_publishes_both_profiles(self):
        self.run_profile('  \"Warm measured baritone.\"  ', expected_failure=False)

    def run_profile(self, content, expected_failure):
        from unittest.mock import Mock
        features = {"mean_f0": 120.0, "std_f0": 10.0, "mean_rms": 0.04,
                    "speaking_rate": 3.0, "mean_centroid": 2000.0, "mean_rolloff": 3000.0,
                    "smoothness": 0.4, "flatness": 0.03, "duration": 5.0}
        existing = {"id": "old", "dataset_id": "narrator_old_voice_book",
                    "zip_source": "old.zip", "voice_profile": "existing profile",
                    "voice_features": copy.deepcopy(features), "custom": {"keep": True}}
        pending = {"id": "new", "dataset_id": "narrator_new_voice_book",
                   "zip_source": "new.zip", "custom": {"keep": 7}}
        provider = Mock()
        provider.create_chat_completion.return_value = {
            "choices": [{"message": {"content": content}}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, model, output = [root / name for name in ("manifest.json", "model.gguf", "profiles.csv")]
            manifest.write_text(json.dumps([existing, pending]), encoding="utf-8")
            model.write_bytes(b"not loaded")
            argv = ["voice_profiler.py", "--manifest", str(manifest), "--model", str(model),
                    "--output_csv", str(output)]
            fake_llama = SimpleNamespace(Llama=lambda **_kwargs: provider)
            with patch.object(sys, "argv", argv), \
                 patch.dict(sys.modules, {"llama_cpp": fake_llama}), \
                 patch.object(voice_profiler, "get_ref_wav", return_value=b"mocked decode"), \
                 patch.object(voice_profiler, "analyze_ref_wav", return_value=features), \
                 patch.object(voice_profiler, "find_epub", return_value=None), \
                 redirect_stdout(io.StringIO()) as logs:
                code = voice_profiler.main()
            self.assertEqual(1 if expected_failure else 0, code)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(existing, saved[0])
            self.assertEqual(pending["custom"], saved[1]["custom"])
            self.assertIn("voice_features", saved[1])
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            if expected_failure:
                self.assertNotIn("voice_profile", saved[1])
                self.assertIn("leaving profile pending", logs.getvalue())
                self.assertEqual(["old"], [row["id"] for row in rows])
            else:
                self.assertEqual("Warm measured baritone.", saved[1]["voice_profile"])
                self.assertEqual(["old", "new"], [row["id"] for row in rows])
                self.assertEqual(saved[1]["voice_profile"], rows[1]["voice_profile"])
            provider.create_chat_completion.assert_called_once()
            request = provider.create_chat_completion.call_args.kwargs
            self.assertEqual(80, request["max_tokens"])
            self.assertEqual(["\n"], request["stop"])
            self.assertEqual(["system", "user"], [message["role"] for message in request["messages"]])
            self.assertEqual(b"not loaded", model.read_bytes())


class ProfilerCsvParentPreflightTests(unittest.TestCase):
    def _preflight(self, root, output):
        manifest = root / "manifest.json"
        model = root / "model.gguf"
        manifest.write_text("[]", encoding="utf-8")
        model.write_bytes(b"model fixture")
        with patch.object(voice_profiler, "DEPENDENCY_ERROR", None), \
             patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=object())}):
            return voice_profiler.get_preflight_report(str(manifest), str(model), str(output), [])

    def test_missing_nested_csv_parent_passes_without_creating_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "new" / "nested" / "profiles.csv"
            report = self._preflight(root, output)
            self.assertEqual("passed", report["status"], report)
            self.assertFalse(output.parent.exists())
            self.assertEqual({"manifest.json", "model.gguf"}, {p.name for p in root.iterdir()})
            voice_profiler.atomic_csv_write([{"id": "known", "voice_profile": "verified"}], str(output))
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("known", rows[0]["id"])
            self.assertEqual("verified", rows[0]["voice_profile"])
            self.assertEqual("[]", (root / "manifest.json").read_text())
            self.assertEqual(b"model fixture", (root / "model.gguf").read_bytes())

    def test_file_ancestor_is_rejected_and_existing_output_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obstacle = root / "file"
            obstacle.write_bytes(b"must not overwrite")
            report = self._preflight(root, obstacle / "nested" / "profiles.csv")
            self.assertEqual("failed", report["status"])
            self.assertTrue(any("output directory" in error for error in report["errors"]))
            self.assertEqual(b"must not overwrite", obstacle.read_bytes())
            existing = root / "profiles.csv"
            existing.write_bytes(b"prior CSV")
            self.assertEqual("passed", self._preflight(root, existing)["status"])
            self.assertEqual(b"prior CSV", existing.read_bytes())
            self.assertFalse(list(root.glob(".voice_profiler_check_*")))

    def test_unwritable_existing_ancestor_fails_without_loading_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "new" / "profiles.csv"
            with patch.object(voice_profiler.tempfile, "mkstemp", side_effect=PermissionError("denied")) as probe:
                report = self._preflight(root, output)
            self.assertEqual("failed", report["status"])
            self.assertTrue(any("output directory" in error for error in report["errors"]))
            self.assertEqual(str(root), probe.call_args.kwargs["dir"])
            self.assertFalse(output.parent.exists())


class LegacyProfilerIdTests(unittest.TestCase):
    def test_legacy_entry_without_id_does_not_stop_actual_cli_or_drop_later_profiles(self):
        import array
        import wave
        from unittest.mock import Mock
        features = {"mean_f0": 120.0, "std_f0": 10.0, "mean_rms": 0.04,
                    "speaking_rate": 3.0, "mean_centroid": 2000.0, "mean_rolloff": 3000.0,
                    "smoothness": 0.4, "flatness": 0.03, "duration": 0.1}
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wav = io.BytesIO()
                with wave.open(wav, "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(array.array('h', [4096] * 2400).tobytes())
                manifest = []
                zip_bytes = {}
                for name in ("legacy", "current"):
                    archive = root / (name + ".zip")
                    with zipfile.ZipFile(archive, "w") as zipped:
                        zipped.writestr("ref.wav", wav.getvalue())
                        zipped.writestr("ref_text.txt", "A matching reference passage.")
                    zip_bytes[archive] = archive.read_bytes()
                    entry = {"dataset_id": "narrator_" + name + "_voice_book", "zip_source": str(archive), "custom": {"keep": name}}
                    if name == "current":
                        entry["id"] = "current"
                    manifest.append(entry)
                manifest_path, model, output = [root / name for name in ("manifest.json", "model.gguf", "profiles.csv")]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                original = manifest_path.read_bytes()
                model.write_bytes(b"model stand-in")
                output.write_bytes(b"prior CSV")
                provider = Mock()
                provider.create_chat_completion.return_value = {"choices": [{"message": {"content": "Warm measured baritone."}}]}
                argv = ["voice_profiler.py", "--manifest", str(manifest_path), "--model", str(model), "--output_csv", str(output)]
                if dry_run:
                    argv.append("--dry_run")
                with patch.object(sys, "argv", argv), \
                     patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=lambda **kwargs: provider)}), \
                     patch.object(voice_profiler, "analyze_ref_wav", return_value=features) as analyze, \
                     patch.object(voice_profiler, "find_epub", return_value=None), \
                     redirect_stdout(io.StringIO()) as logs:
                    self.assertEqual(0, voice_profiler.main())
                self.assertEqual(2, analyze.call_count)
                self.assertTrue(all(call.args[0] == wav.getvalue() for call in analyze.call_args_list))
                self.assertIn("Done: 2 profiles processed, 0 errors", logs.getvalue())
                if dry_run:
                    self.assertEqual(original, manifest_path.read_bytes())
                    self.assertEqual(b"prior CSV", output.read_bytes())
                    provider.create_chat_completion.assert_not_called()
                else:
                    saved = json.loads(manifest_path.read_text())
                    self.assertNotIn("id", saved[0])
                    self.assertEqual(["legacy", "current"], [entry["custom"]["keep"] for entry in saved])
                    self.assertTrue(all(entry["voice_profile"] == "Warm measured baritone." for entry in saved))
                    with output.open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(["", "current"], [row["id"] for row in rows])
                    self.assertEqual(2, provider.create_chat_completion.call_count)
                for archive, before in zip_bytes.items():
                    self.assertEqual(before, archive.read_bytes())
                self.assertEqual(b"model stand-in", model.read_bytes())


class BatchMetadataFailureTests(unittest.TestCase):
    def test_actual_cli_cleans_bad_encoding_and_continues_to_next_zip(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zips = root/'zips'
            zips.mkdir()
            for name, data in (('a_bad.zip',b'\xffinvalid utf8'),('b_good.zip',b'{"text":"CPU fixture"}\n')):
                with zipfile.ZipFile(zips/name,'w') as archive:
                    archive.writestr('metadata.jsonl',data)
            child = root/'cpu_training_fixture.py'
            child.write_text("\n".join([
                'import argparse,json,pathlib',
                'parser=argparse.ArgumentParser()',
                'parser.add_argument("--output_dir");parser.add_argument("--data_dir")',
                'args,_=parser.parse_known_args()',
                'root=pathlib.Path(args.output_dir);root.mkdir(parents=True,exist_ok=True)',
                '(root/"training_meta.json").write_text(json.dumps({"epochs":1,"final_loss":3.0,"best_loss":3.0}))',
                'from safetensors.numpy import save_file;import numpy as np',
                '(root/"adapter_config.json").write_text(json.dumps({"peft_type":"LORA","r":2,"lora_alpha":4,"target_modules":["q_proj"]}))',
                'save_file({"layer.lora_A.weight":np.ones((2,3),dtype=np.float32)},str(root/"adapter_model.safetensors"))',
                'print("[DONE] CPU fixture completed",flush=True)',
            ])+'\n')
            datasets,models,manifest = root/'datasets',root/'models',root/'manifest.json'
            result = subprocess.run([sys.executable,str(ROOT/'tools/voice_lab/batch_train_lora.py'),
                '--zips_dir',str(zips),'--datasets_dir',str(datasets),'--models_dir',str(models),
                '--manifest',str(manifest),'--train_script',str(child),'--python',sys.executable],
                capture_output=True,text=True,timeout=10)
            self.assertEqual(1,result.returncode,result.stdout+result.stderr)
            self.assertIn('Done: 1 trained, 0 skipped, 1 errors',result.stdout)
            self.assertIn('ERROR reading metadata',result.stdout)
            self.assertNotIn('Traceback',result.stderr)
            self.assertEqual([],list(datasets.iterdir()))
            rows=json.loads(manifest.read_text())
            self.assertEqual(1,len(rows))
            self.assertEqual('b_good',rows[0]['dataset_id'])
            self.assertEqual(1,rows[0]['sample_count'])
            self.assertEqual({rows[0]['id'], 'manifest.json.lock'},
                             {p.name for p in models.iterdir()})
            self.assertTrue((models/'manifest.json.lock').is_file())
            self.assertEqual(3.0,json.loads((models/rows[0]['id']/'training_meta.json').read_text())['best_loss'])
            with zipfile.ZipFile(zips/'a_bad.zip') as archive:
                self.assertEqual(b'\xffinvalid utf8',archive.read('metadata.jsonl'))

    def test_metadata_read_failure_cleans_dataset_but_preserves_existing_output(self):
        for as_directory in (False,True):
            with self.subTest(directory=as_directory),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                archive=root/'bad.zip'
                with zipfile.ZipFile(archive,'w') as zipped:
                    zipped.writestr('metadata.jsonl/' if as_directory else 'metadata.jsonl',
                                    b'' if as_directory else b'\xffbad')
                args=SimpleNamespace(datasets_dir=str(root/'datasets'),models_dir=str(root/'models'))
                output=Path(args.models_dir,'existing')
                output.mkdir(parents=True)
                sentinel=output/'keep.bin'
                sentinel.write_bytes(b'prior adapter bytes')
                with patch.object(batch_train.subprocess,'Popen') as process,redirect_stdout(io.StringIO()) as log:
                    result=batch_train.train_one(str(archive),'bad','existing',args)
                self.assertIsNone(result)
                process.assert_not_called()
                self.assertFalse((Path(args.datasets_dir)/'bad').exists())
                self.assertEqual(b'prior adapter bytes',sentinel.read_bytes())
                self.assertIn('ERROR reading metadata',log.getvalue())


class BatchTrainingEtaTests(unittest.TestCase):
    def test_eta_counts_attempted_training_instead_of_cached_zip_ordinals(self):
        for fail_first in (False,True):
            with self.subTest(fail_first=fail_first),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                zips=root/'zips'
                zips.mkdir()
                for index in range(12):
                    (zips/('%02d.zip' % index)).touch()
                clock=[0.0]
                cache_checks=[]
                attempted=[]
                def cached(models,dataset_id,manifest):
                    cache_checks.append(dataset_id)
                    return 'cached' if len(cache_checks)<=9 else None
                def train(zip_path,dataset_id,adapter_id,args):
                    attempted.append(dataset_id)
                    clock[0]+=300.0  # Controlled fixture duration, not a GPU measurement.
                    if fail_first and len(attempted)==1:
                        return None
                    return {'id':adapter_id,'dataset_id':dataset_id}
                argv=['batch_train_lora.py','--zips_dir',str(zips),
                    '--datasets_dir',str(root/'datasets'),'--models_dir',str(root/'models'),
                    '--manifest',str(root/'manifest.json')]
                with patch.object(sys,'argv',argv), \
                     patch.object(batch_train,'adapter_exists',side_effect=cached), \
                     patch.object(batch_train,'train_one',side_effect=train), \
                     patch.object(batch_train.time,'time',side_effect=lambda:clock[0]), \
                     redirect_stdout(io.StringIO()) as log:
                    code=batch_train.main()
                self.assertEqual(int(fail_first),code)
                self.assertEqual(3,len(attempted))
                progress=[line.strip() for line in log.getvalue().splitlines() if 'Progress:' in line]
                if fail_first:
                    self.assertEqual('Progress: 1 done, 9 skipped, 1 errors — ETA: 5 min for 1 remaining',progress[0])
                else:
                    self.assertEqual('Progress: 1 done, 9 skipped, 0 errors — ETA: 10 min for 2 remaining',progress[0])
                    self.assertEqual('Progress: 2 done, 9 skipped, 0 errors — ETA: 5 min for 1 remaining',progress[1])
                self.assertIn('ETA: 0 min for 0 remaining',progress[-1])
                self.assertEqual(2 if fail_first else 3,len(json.loads((root/'manifest.json').read_text())))


class ProfilerEpubNavigationTests(unittest.TestCase):
    paragraph = "This is the expected book passage. " + "The narrator read these ordinary words aloud. " * 6

    def build_epub(self, path, quote='"', href="chapter.xhtml", member="OEBPS/chapter.xhtml",
                   reordered=False, namespace=False, full_path="OEBPS/content.opf",
                   malformed_container=False, malformed_opf=False, missing=False):
        q = quote
        ns = ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"' if namespace else ''
        container = f'<container{ns}><rootfiles><rootfile full-path={q}{full_path}{q}/></rootfiles></container>'
        item = (f'<item href={q}{href}{q} id={q}chapter{q}/>' if reordered else
                f'<item id={q}chapter{q} href={q}{href}{q}/>')
        ref = (f'<itemref linear={q}yes{q} idref={q}chapter{q}/>' if reordered else
               f'<itemref idref={q}chapter{q}/>')
        ns = ' xmlns="http://www.idpf.org/2007/opf"' if namespace else ''
        opf = f'<package{ns}><manifest>{item}</manifest><spine>{ref * 4}</spine></package>'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/container.xml", "<broken" if malformed_container else container)
            archive.writestr("OEBPS/content.opf", "<broken" if malformed_opf else opf)
            if not missing:
                archive.writestr(member, "<html><body><p>" + self.paragraph + "</p></body></html>")

    def test_valid_xml_and_local_uri_variants_return_exact_passage_without_zip_changes(self):
        cases = [{}, {"quote": "'"}, {"reordered": True}, {"namespace": True},
                 {"quote": "'", "reordered": True, "namespace": True},
                 {"href": "../Text/chapter.xhtml", "member": "Text/chapter.xhtml"},
                 {"href": "./Text/../chapter.xhtml"},
                 {"href": "chapter%20one.xhtml", "member": "OEBPS/chapter one.xhtml"},
                 {"href": "chapter%20%E5%A3%B0.xhtml#paragraph?ignored", "member": "OEBPS/chapter 声.xhtml"},
                 {"href": "chapter.xhtml?query=yes#paragraph"},
                 {"member": "chapter.xhtml"},
                 {"full_path": "./OEBPS/../OEBPS/content.opf#package"},
                 {"full_path": "OEBPS/content%2Eopf"}]
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "book.epub")
                self.build_epub(path, **options)
                before = path.read_bytes()
                expected = ((self.paragraph.strip() + " ") * 3)[:600].strip()
                self.assertEqual(expected, voice_profiler.extract_epub_passage(str(path)))
                self.assertEqual(expected[:100].strip(), voice_profiler.extract_epub_passage(str(path), 100))
                self.assertEqual(before, path.read_bytes())

    def test_invalid_or_external_navigation_returns_empty_without_reading_unrelated_member(self):
        cases = [{"malformed_container": True}, {"malformed_opf": True}, {"missing": True},
                 {"href": "https://example.test/chapter.xhtml", "member": "chapter.xhtml"},
                 {"href": "//example.test/chapter.xhtml", "member": "chapter.xhtml"},
                 {"href": "../../chapter.xhtml", "member": "chapter.xhtml"},
                 {"href": "%2E%2E/%2E%2E/chapter.xhtml", "member": "chapter.xhtml"},
                 {"full_path": "../OEBPS/content.opf"},
                 {"full_path": "https://example.test/OEBPS/content.opf"}]
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "book.epub")
                self.build_epub(path, **options)
                before = path.read_bytes()
                self.assertEqual("", voice_profiler.extract_epub_passage(str(path)))
                self.assertEqual(before, path.read_bytes())
