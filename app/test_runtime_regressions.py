import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app as app_module
import core as core_module
from routers import preparer as preparer_module
from routers import lora as lora_module
from routers import voice_design as voice_design_module
from routers import scripts_library as scripts_library_module
from routers import system as system_module
from routers import voicelab as voicelab_module
import utils
import hf_utils
from test_support import _Upload


class RuntimeTests(unittest.TestCase):
    def test_app_import_does_not_run_stuck_chunk_recovery(self):
        with patch.object(core_module.project_manager, "load_chunks") as load_chunks:
            importlib.reload(app_module)

        load_chunks.assert_not_called()

    def test_reset_stuck_chunks_changes_only_generating_statuses(self):
        chunks = [
            {"status": "generating", "text": "first"},
            {"status": "pending", "text": "second"},
            {"status": "done", "text": "third"},
            {"status": "error", "text": "fourth"},
            {"status": "generating", "text": "fifth"},
        ]
        with patch.object(core_module.project_manager, "load_chunks", return_value=chunks), \
             patch.object(core_module.project_manager, "save_chunks") as save_chunks:
            reset_count = app_module.reset_stuck_chunks()

        self.assertEqual(2, reset_count)
        self.assertEqual(
            ["pending", "pending", "done", "error", "pending"],
            [chunk["status"] for chunk in chunks],
        )
        save_chunks.assert_called_once_with(chunks)

    def test_reset_stuck_chunks_does_not_save_when_nothing_changed(self):
        for chunks in ([], [{"status": "pending"}, {"status": "done"}]):
            with self.subTest(chunks=chunks), \
                 patch.object(core_module.project_manager, "load_chunks", return_value=chunks), \
                 patch.object(core_module.project_manager, "save_chunks") as save_chunks:
                self.assertEqual(0, app_module.reset_stuck_chunks())
                save_chunks.assert_not_called()

    def test_app_lifespan_runs_stuck_chunk_recovery_once(self):
        async def run_lifespan():
            async with app_module.app.router.lifespan_context(app_module.app):
                pass

        with patch.object(app_module, "reset_stuck_chunks") as reset_stuck_chunks:
            asyncio.run(run_lifespan())

        reset_stuck_chunks.assert_called_once_with()

    def test_system_stats_does_not_block_eta_requests(self):
        async def exercise_requests():
            probe_started = threading.Event()
            release_probe = threading.Event()

            def slow_gpu_probe():
                probe_started.set()
                if not release_probe.wait(timeout=3):
                    raise AssertionError("ETA request could not run while GPU probe was blocked")
                return None

            transport = ASGITransport(app=app_module.app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                with patch.object(system_module, "get_gpu_stats", side_effect=slow_gpu_probe), \
                     patch.object(system_module, "check_disk_space", return_value=(True, 10.0)), \
                     patch.object(system_module, "_get_torch", return_value=None), \
                     patch.object(system_module, "run_rocm_smi_json", return_value=None), \
                     patch.object(system_module, "system_has_gpu", return_value=(False, None)):
                    stats_request = asyncio.create_task(client.get("/api/system/stats"))
                    try:
                        probe_ready = await asyncio.to_thread(probe_started.wait, 1)
                        self.assertTrue(probe_ready, "system-stats probe did not start")
                        eta_response = await asyncio.wait_for(
                            client.get("/api/status/eta"), timeout=1
                        )
                    finally:
                        release_probe.set()

                    stats_response = await asyncio.wait_for(stats_request, timeout=1)

            self.assertEqual(200, eta_response.status_code)
            self.assertEqual(200, stats_response.status_code)

        asyncio.run(exercise_requests())

    def test_gpu_stats_cache_preserves_rocm_fallback_result(self):
        fallback_stats = {
            "allocated_gb": 1.0,
            "reserved_gb": 1.0,
            "total_gb": 8.0,
            "allocated_percent": 12.5,
            "utilization_percent": 25.0,
        }
        with patch.dict(system_module._gpu_stats_cache,
                        {"data": None, "timestamp": 0}, clear=True), \
             patch.object(system_module.time, "time", side_effect=(100.0, 101.0)), \
             patch.object(system_module, "_get_torch", return_value=None), \
             patch.object(system_module, "_gpu_stats_via_rocm_smi",
                          return_value=fallback_stats) as rocm_fallback:
            first = system_module.get_gpu_stats()
            second = system_module.get_gpu_stats()

        self.assertEqual(fallback_stats, first)
        self.assertIs(first, second)
        rocm_fallback.assert_called_once_with()

    def test_runtime_data_dir_ignores_empty_environment_override(self):
        with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": "   "}):
            self.assertEqual(utils.get_runtime_data_dir("/expected/root"),
                             "/expected/root")

    def test_voice_design_claims_gpu_before_engine_initialization(self):
        core_module.process_state["audio"]["running"] = True
        try:
            with patch.object(core_module.project_manager, "get_engine") as get_engine:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(voice_design_module.voice_design_preview(
                        voice_design_module.VoiceDesignPreviewRequest(
                            description="voice", sample_text="text", language="english")))
            self.assertEqual(raised.exception.status_code, 400)
            get_engine.assert_not_called()
        finally:
            core_module.process_state["audio"]["running"] = False

    def test_lora_epochs_must_be_positive_at_api_boundary(self):
        for epochs in (0, -1):
            with self.assertRaises(ValueError):
                lora_module.LoraTrainingRequest(name="x", dataset_id="d", epochs=epochs)

    def test_builtin_manifest_normalization_skips_bad_entries(self):
        entries = hf_utils._normalize_manifest_entries([
            "bad", {}, {"id": " good ", "name": 42, "final_loss": "bad"}
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "good")
        self.assertEqual(entries[0]["name"], "42")
        self.assertIsNone(entries[0]["final_loss"])

    def test_lora_style_attribute_escapes_persisted_content(self):
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("value=\"${escapeHtml(voiceType === 'lora' ? (config.character_style || '') : '')}\"", html)
        self.assertIn("No downloaded voice matched the known gender; fallback used.", html)
        self.assertIn("Existing recurring voice retained despite a trait mismatch.", html)
        self.assertIn("escapeHtml(sugg.trait_evidence)", html)
        self.assertIn("escapeHtml(config.ref_text || '')", html)
        self.assertIn("escapeHtml(config.ref_audio || '')", html)
        self.assertIn("escapeHtml(config.description || '')", html)
        self.assertNotIn("onclick='downloadBuiltinAdapter(${JSON.stringify(m.id)})'", html)

    def test_queued_cancel_survives_background_start(self):
        key = "_test_claimed_task"
        core_module.process_state[key] = {
            "running": False, "cancel": True, "logs": [], "process": None,
        }
        core_module.GPU_TASKS.add(key)
        try:
            core_module.claim_gpu_task(key)
            self.assertFalse(core_module.process_state[key]["cancel"])
            core_module.process_state[key]["cancel"] = True
            observed = []
            core_module._run_claimed_background_task(
                key, lambda: observed.append(core_module.process_state[key]["cancel"])
            )
            self.assertEqual(observed, [True])
            self.assertFalse(core_module.process_state[key]["running"])
        finally:
            core_module.GPU_TASKS.discard(key)
            core_module.process_state.pop(key, None)

    def test_failed_claimed_task_releases_running_state(self):
        key = "_test_failed_task"
        core_module.process_state[key] = {"running": True, "logs": [], "process": None}
        try:
            core_module._run_claimed_background_task(
                key, lambda: (_ for _ in ()).throw(OSError("launch failed"))
            )
            self.assertFalse(core_module.process_state[key]["running"])
            self.assertIn("launch failed", core_module.process_state[key]["logs"][-1])
        finally:
            core_module.process_state.pop(key, None)

    def test_oversized_upload_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "upload.bin")
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(core_module._save_upload_limited(
                    _Upload([b"1234", b"5678"]), path, 6
                ))
            self.assertEqual(raised.exception.status_code, 413)
            self.assertFalse(os.path.exists(path))

    def test_preparer_rejects_unimplemented_skip_before_startup(self):
        config = {
            "audio_filename": "book.wav",
            "output_filename": "dataset.zip",
            "skip_annotation": True,
        }
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(preparer_module.preparer_start(
                None, json.dumps(config), None, None
            ))
        self.assertEqual(raised.exception.status_code, 400)

    def test_voicelab_defaults_do_not_guess_paths_inside_this_repo(self):
        # rocm_python and pipeline_repo live outside this repo (a separate ML
        # env + the repo holding batch_train_lora.py). Deriving them from
        # ROOT_DIR only ships paths that cannot resolve.
        for key in ("rocm_python", "pipeline_repo"):
            value = core_module.VOICELAB_DEFAULTS[key]
            self.assertFalse(
                value.startswith(core_module.ROOT_DIR),
                f"{key} default must not be guessed from ROOT_DIR, got {value!r}",
            )

    def _voicelab_start_with(self, cfg_overrides):
        cfg = dict(core_module.VOICELAB_DEFAULTS)
        cfg.update(cfg_overrides)
        request = voicelab_module.VoiceLabRequest(stages=["train"])
        with patch.object(voicelab_module, "_load_voicelab_config", return_value=cfg):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(voicelab_module.voicelab_start(request, None))
        return raised.exception

    def test_voicelab_start_reports_unconfigured_rocm_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            exc = self._voicelab_start_with(
                {"zips_dir": tmp, "rocm_python": "", "pipeline_repo": tmp})
        self.assertEqual(exc.status_code, 400)
        self.assertIn("not configured", exc.detail)

    def test_voicelab_start_reports_unconfigured_pipeline_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            interpreter = os.path.join(tmp, "python")
            Path(interpreter).write_text("", encoding="utf-8")
            os.chmod(interpreter, 0o755)
            # train reads _deduped; create it so the run reaches the repo check
            os.makedirs(os.path.join(tmp, "_deduped"))
            exc = self._voicelab_start_with(
                {"zips_dir": tmp, "rocm_python": interpreter, "pipeline_repo": ""})
        self.assertEqual(exc.status_code, 400)
        self.assertIn("not configured", exc.detail)

    def test_pair_write_rolls_back_first_file_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = os.path.join(tmp, "first.json"), os.path.join(tmp, "second.json")
            Path(first).write_text('{"old": 1}', encoding="utf-8")
            Path(second).write_text('{"old": 2}', encoding="utf-8")
            real_replace = os.replace
            def fail_second(src, dst):
                if dst == second and os.path.basename(src).startswith(".pair-"):
                    raise OSError("disk failure")
                return real_replace(src, dst)
            with patch.object(utils.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    utils.atomic_json_write_pair({"new": 1}, first, {"new": 2}, second)
            self.assertEqual(json.loads(Path(first).read_text()), {"old": 1})
            self.assertEqual(json.loads(Path(second).read_text()), {"old": 2})

    def test_saved_book_metadata_preserves_original_identity(self):
        with tempfile.TemporaryDirectory() as tmp, \
            patch.object(core_module, "SCRIPTS_DIR", tmp):
            utils.atomic_json_write({"book_id": "original-upload"},
                                    core_module._saved_book_meta_path("volume-1"))
            self.assertEqual(core_module._get_saved_book_id("volume-1"), "original-upload")

    def test_saved_book_metadata_is_not_listed_as_a_script(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scripts_library_module, "SCRIPTS_DIR", tmp):
            Path(tmp, "volume-1.json").write_text("[]", encoding="utf-8")
            Path(tmp, "volume-1.meta.json").write_text('{"book_id":"original"}', encoding="utf-8")
            listed = asyncio.run(scripts_library_module.list_saved_scripts())
        self.assertEqual([item["name"] for item in listed], ["volume-1"])

    def test_runtime_data_dir_isolates_mutable_app_paths(self):
        root = Path(__file__).resolve().parent.parent
        code = (
            "import core; print(core.DATA_DIR); print(core.SCRIPT_PATH); "
            "print(core.VOICE_LIBRARY_PATH); print(core.CONFIG_PATH); print(core.UPLOADS_DIR)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ALEXANDRIA_DATA_DIR=tmp)
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=root / "app", env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
            self.assertTrue(paths)
            for path in paths:
                self.assertTrue(utils.is_path_inside(path, tmp), path)

    def test_lora_cancel_idle_reports_not_running(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(lora_module.lora_cancel_training())
        self.assertEqual(raised.exception.status_code, 400)
