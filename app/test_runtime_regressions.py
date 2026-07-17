import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app as app_module
import archive_utils
import core as core_module
import device_utils
import tts as tts_module
import tts_vram_benchmark as tts_benchmark_module
from routers import preparer as preparer_module
from routers import script as script_module
from routers import dataset_builder as dataset_builder_module
from routers import lora as lora_module
from routers import voice_design as voice_design_module
from routers import scripts_library as scripts_library_module
from routers import system as system_module
from routers import voicelab as voicelab_module
import utils
import hf_utils
from test_support import _Upload


class RuntimeTests(unittest.TestCase):
    def test_concurrent_identical_uploads_keep_one_canonical_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = os.path.join(tmp, "uploads")
            os.makedirs(uploads)

            async def upload():
                source = _Upload([b"same concurrent book"])
                source.filename = "book.txt"
                return await script_module.upload_file(source)

            async def upload_both():
                return await asyncio.gather(upload(), upload())

            with patch.object(script_module, "UPLOADS_DIR", uploads), \
                 patch.object(script_module, "DATA_DIR", tmp):
                results = asyncio.run(upload_both())

            self.assertEqual([False, True], sorted(result["reused"] for result in results))
            self.assertEqual(1, len(os.listdir(uploads)))
            self.assertEqual(results[0]["stored_filename"], results[1]["stored_filename"])

    def test_identical_script_upload_reuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = os.path.join(tmp, "uploads")
            os.makedirs(uploads)

            async def upload(filename):
                source = _Upload([b"same book"])
                source.filename = filename
                return await script_module.upload_file(source)

            with patch.object(script_module, "UPLOADS_DIR", uploads), \
                 patch.object(script_module, "DATA_DIR", tmp):
                first = asyncio.run(upload("book.txt"))
                second = asyncio.run(upload("book.txt"))

            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["stored_filename"], second["stored_filename"])
            self.assertEqual(["book.txt"], os.listdir(uploads))

    def test_existing_upload_can_be_listed_and_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = os.path.join(tmp, "uploads")
            os.makedirs(uploads)
            Path(uploads, "book.txt").write_text("book", encoding="utf-8")
            Path(uploads, "ignore.epub").write_bytes(b"archive")

            with patch.object(script_module, "UPLOADS_DIR", uploads), \
                 patch.object(script_module, "DATA_DIR", tmp):
                listed = script_module._get_reusable_uploads()
                selected = script_module._select_upload("book.txt")

            self.assertEqual(["book.txt"], [item["filename"] for item in listed])
            self.assertEqual(64, len(listed[0]["sha256"]))
            self.assertEqual(os.path.join(uploads, "book.txt"), selected)
            state = json.loads(Path(tmp, "state.json").read_text(encoding="utf-8"))
            self.assertEqual(selected, state["input_file_path"])

    def test_script_collision_helpers_version_and_backup_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "book.json")
            Path(script).write_text("old", encoding="utf-8")
            Path(tmp, "book_2.json").write_text("version", encoding="utf-8")

            versioned = script_module._get_versioned_script_path(script)
            backup = utils.backup_file_with_timestamp(script)

            self.assertEqual(os.path.join(tmp, "book_3.json"), versioned)
            self.assertEqual("old", Path(backup).read_text(encoding="utf-8"))
            self.assertEqual("old", Path(script).read_text(encoding="utf-8"))

    def test_dataset_builder_save_rejects_done_sample_with_missing_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder_root = os.path.join(tmp, "builder")
            datasets_root = os.path.join(tmp, "datasets")
            work_dir = os.path.join(builder_root, "voice")
            os.makedirs(work_dir)
            Path(work_dir, "state.json").write_text(json.dumps({
                "samples": [{"status": "done", "text": "Missing audio"}],
            }), encoding="utf-8")

            with patch.object(dataset_builder_module, "DATASET_BUILDER_DIR", builder_root), \
                 patch.object(dataset_builder_module, "LORA_DATASETS_DIR", datasets_root):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(dataset_builder_module.dataset_builder_save(
                        dataset_builder_module.DatasetSaveRequest(name="voice", ref_index=0)))

            self.assertEqual(400, raised.exception.status_code)
            self.assertIn("sample_000.wav", raised.exception.detail)
            self.assertFalse(os.path.exists(os.path.join(datasets_root, "voice")))

    def test_same_name_designed_voices_get_independent_ids_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            previews = os.path.join(tmp, "previews")
            os.makedirs(previews)
            Path(previews, "first.wav").write_bytes(b"first")
            Path(previews, "second.wav").write_bytes(b"second")
            manifest_path = os.path.join(tmp, "manifest.json")

            async def save(preview_file):
                return await voice_design_module.voice_design_save(
                    voice_design_module.VoiceDesignSaveRequest(
                        name="Same Voice", description="test", sample_text="test",
                        preview_file=preview_file))

            with patch.object(voice_design_module, "DESIGNED_VOICES_DIR", tmp), \
                 patch.object(voice_design_module, "DESIGNED_VOICES_MANIFEST", manifest_path):
                first = asyncio.run(save("first.wav"))
                second = asyncio.run(save("second.wav"))

            self.assertNotEqual(first["voice_id"], second["voice_id"])
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(2, len(manifest))
            self.assertEqual(b"first", Path(tmp, manifest[0]["filename"]).read_bytes())
            self.assertEqual(b"second", Path(tmp, manifest[1]["filename"]).read_bytes())

    def test_same_name_clone_uploads_get_independent_ids_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")

            async def upload(content):
                source = _Upload([content])
                source.filename = "Same Voice.wav"
                return await voice_design_module.clone_voices_upload(source)

            with patch.object(voice_design_module, "CLONE_VOICES_DIR", tmp), \
                 patch.object(voice_design_module, "CLONE_VOICES_MANIFEST", manifest_path):
                first = asyncio.run(upload(b"first"))
                second = asyncio.run(upload(b"second"))

            self.assertNotEqual(first["voice_id"], second["voice_id"])
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(b"first", Path(tmp, manifest[0]["filename"]).read_bytes())
            self.assertEqual(b"second", Path(tmp, manifest[1]["filename"]).read_bytes())

    def test_lora_zip_validation_rejects_insufficient_extraction_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            member = SimpleNamespace(filename="audio.wav", file_size=10)
            archive = SimpleNamespace(infolist=lambda: [member], extractall=Mock())
            with patch.object(archive_utils.shutil, "disk_usage",
                              return_value=SimpleNamespace(free=5)), \
                 self.assertRaises(HTTPException) as raised:
                lora_module._safe_extractall(archive, tmp)

            self.assertEqual(400, raised.exception.status_code)
            self.assertIn("available extraction disk", raised.exception.detail)
            archive.extractall.assert_not_called()

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

    def test_app_lifespan_prunes_abandoned_review_sessions(self):
        # Abandoned blind-review sessions must be cleared at startup, not only
        # when the next session is opened.
        async def run_lifespan():
            async with app_module.app.router.lifespan_context(app_module.app):
                pass

        with patch.object(app_module.evaluation_reviews, "prune_sessions") as prune:
            with patch.object(app_module, "reset_stuck_chunks"):
                asyncio.run(run_lifespan())

        prune.assert_called_once_with(app_module.EVALUATION_REVIEWS_DIR)

    def test_index_stamps_served_build_for_stale_tab_detection(self):
        # The served page must carry the current build so a tab open across a
        # backend update can detect it is stale (Phase 7).
        with patch.object(system_module, "get_runtime_info",
                          return_value={"short_revision": "abc1234"}):
            response = asyncio.run(system_module.read_index())
        body = response.body.decode("utf-8")
        self.assertIn('<meta name="app-build" content="abc1234">', body)
        # Only the meta tag is stamped — the JS placeholder literal the frontend
        # compares against MUST survive, or a blanket replace would rewrite the
        # guard (PAGE_BUILD !== '<placeholder>') and permanently disable
        # detection on served pages.
        self.assertIn("'__APP_BUILD__'", body)
        self.assertNotIn('content="__APP_BUILD__"', body)

    def test_index_stamps_empty_build_when_revision_unavailable(self):
        # An unknown revision must render an empty stamp (informational), not the
        # literal placeholder in the meta tag, so the frontend never
        # false-positives a mismatch.
        with patch.object(system_module, "get_runtime_info", return_value={}):
            response = asyncio.run(system_module.read_index())
        body = response.body.decode("utf-8")
        self.assertIn('<meta name="app-build" content="">', body)
        self.assertNotIn('content="__APP_BUILD__"', body)

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

    def test_voice_design_logs_unexpected_failure_and_returns_sanitized_500(self):
        request = voice_design_module.VoiceDesignPreviewRequest(
            description="voice", sample_text="text", language="english")
        with patch.object(core_module.project_manager, "get_engine",
                          side_effect=RuntimeError("internal model path")), \
             patch.object(voice_design_module.logger, "exception") as log_exception:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(voice_design_module.voice_design_preview(request))

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(
            raised.exception.detail,
            "Voice design preview failed — see server logs for details.")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        log_exception.assert_called_once_with("Voice design preview failed")
        self.assertFalse(core_module.process_state["voice_design"]["running"])

    def test_lora_hyperparameters_are_validated_at_api_boundary(self):
        invalid_values = {
            "epochs": (0, 1001),
            "lr": (0, float("nan"), float("inf"), 1.1),
            "batch_size": (0, 65),
            "lora_r": (0, 1025),
            "lora_alpha": (0, 4097),
            "gradient_accumulation_steps": (0, 1025),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    lora_module.LoraTrainingRequest(
                        name="x", dataset_id="d", **{field: value})

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

    def test_cancel_escalates_process_group_after_grace_period(self):
        process = SimpleNamespace(pid=123)
        with patch.object(core_module, "_send_signal_tree") as send_signal, \
             patch.object(core_module.time, "monotonic",
                          side_effect=[10.0, 19.9, 20.0, 21.0]):
            requested_at, killed = core_module.apply_cancel_escalation(
                process, None, False)
            requested_at, killed = core_module.apply_cancel_escalation(
                process, requested_at, killed)
            requested_at, killed = core_module.apply_cancel_escalation(
                process, requested_at, killed)
            core_module.apply_cancel_escalation(process, requested_at, killed)

        self.assertEqual([signal.SIGTERM, signal.SIGKILL],
                         [call.args[1] for call in send_signal.call_args_list])
        self.assertTrue(killed)

    @unittest.skipUnless(os.name == "posix", "SIGTERM-ignore behavior is POSIX-specific")
    def test_cancel_force_stops_subprocess_that_ignores_sigterm(self):
        state = {"cancel": True, "logs": [], "process": None, "pid": None,
                 "paused": False}
        code = ("import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)")
        with patch.object(core_module, "CANCEL_TERMINATE_GRACE_SECONDS", 0.1):
            return_code, lines = core_module._stream_subprocess_to_logs(
                [sys.executable, "-c", code], os.getcwd(), state)

        self.assertEqual(-signal.SIGKILL, return_code)
        self.assertEqual(["ready"], lines)

    def test_failed_claimed_task_releases_running_state(self):
        key = "_test_failed_task"
        core_module.process_state[key] = {"running": True, "logs": [], "process": None}
        try:
            with tempfile.TemporaryDirectory() as history_dir, \
                 patch.object(core_module, "RUN_HISTORY_DIR", history_dir):
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
        # rocm_python is a separate ML env living outside this repo. Deriving it
        # from ROOT_DIR only ships a path that cannot resolve.
        value = core_module.VOICELAB_DEFAULTS["rocm_python"]
        self.assertFalse(
            value.startswith(core_module.ROOT_DIR),
            f"rocm_python default must not be guessed from ROOT_DIR, got {value!r}",
        )

    def test_voicelab_stage_scripts_ship_with_this_repo(self):
        # train/profile run scripts this repo owns; Voice Lab has no setting to
        # point them elsewhere, so a miss is a broken install.
        for fname in ("audit_voice_datasets.py", "batch_train_lora.py", "evaluate_lora.py", "voice_profiler.py",
                      "voice_analysis.py", "name_voices.py"):
            self.assertTrue(
                os.path.isfile(os.path.join(core_module.ROOT_DIR, fname)),
                f"{fname} must ship with this repo",
            )

    # ── Ensemble ("X and Y" lines voiced by several characters at once) ──

    def _write_tone(self, path, seconds, freq=220.0, sr=24000):
        t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
        sf.write(path, (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr)
        return path

    def test_voice_category_routes_ensemble(self):
        self.assertEqual(tts_module.voice_category({"type": "ensemble"}), "ensemble")

    def test_mix_to_unison_aligns_and_mixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._write_tone(os.path.join(tmp, "a.wav"), 1.0, 220.0)
            b = self._write_tone(os.path.join(tmp, "b.wav"), 1.2, 330.0)
            out = os.path.join(tmp, "out.wav")
            tts_module.mix_to_unison([a, b], out)

            mixed, sr = sf.read(out)
            longest = max(len(sf.read(p)[0]) for p in (a, b))
            # Aligned to the longest clip: they start and end together.
            self.assertEqual(len(mixed), longest)
            # Mixing must not clip.
            self.assertLessEqual(float(np.abs(mixed).max()), 1.0)
            # Both voices must actually be present, not one silently dropped.
            spectrum = np.abs(np.fft.rfft(mixed))
            freqs = np.fft.rfftfreq(len(mixed), 1 / sr)
            for expected in (220.0, 330.0):
                band = spectrum[(freqs > expected - 15) & (freqs < expected + 15)]
                self.assertGreater(band.max(), spectrum.mean() * 10,
                                   f"{expected}Hz voice missing from the mix")

    def test_mix_to_unison_leaves_extreme_outlier_unstretched(self):
        # Stretching a 3x-shorter clip that far smears it audibly; drifting is
        # the better failure. Output still spans the longest clip.
        with tempfile.TemporaryDirectory() as tmp:
            short = self._write_tone(os.path.join(tmp, "s.wav"), 1.0)
            long = self._write_tone(os.path.join(tmp, "l.wav"), 3.0)
            out = os.path.join(tmp, "out.wav")
            tts_module.mix_to_unison([short, long], out, max_stretch=1.35)
            self.assertEqual(len(sf.read(out)[0]), len(sf.read(long)[0]))

    def _ensemble_engine(self):
        return tts_module.TTSEngine({"tts": {}})

    def test_ensemble_renders_each_member_with_its_own_voice(self):
        # The point of the feature: members are ordinary speaker keys, so a
        # member that already has a LoRA is rendered through its LoRA.
        engine = self._ensemble_engine()
        voice_config = {
            "Petra and Subaru": {"type": "ensemble", "members": ["Petra", "Subaru"]},
            "Petra": {"type": "lora", "adapter_path": "petra.safetensors"},
            "Subaru": {"type": "custom", "voice": "Ryan"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            rendered = []

            def fake_generate_voice(text, instruct, speaker, cfg, path):
                rendered.append(speaker)
                self._write_tone(path, 1.0 + 0.1 * len(rendered))
                return True

            out = os.path.join(tmp, "out.wav")
            with patch.object(engine, "generate_voice", side_effect=fake_generate_voice):
                ok = engine.generate_ensemble_voice(
                    "Together!", "", voice_config["Petra and Subaru"], voice_config, out)
            self.assertTrue(ok)
            self.assertEqual(rendered, ["Petra", "Subaru"])
            self.assertTrue(os.path.isfile(out))

    def test_ensemble_rejects_nested_ensemble(self):
        # Must fail loudly rather than recurse forever.
        engine = self._ensemble_engine()
        voice_config = {
            "A": {"type": "ensemble", "members": ["B"]},
            "B": {"type": "ensemble", "members": ["A"]},
        }
        with self.assertRaises(ValueError) as raised:
            engine.generate_ensemble_voice("hi", "", voice_config["A"], voice_config, "x.wav")
        self.assertIn("nested", str(raised.exception))

    def test_ensemble_rejects_unknown_or_empty_members(self):
        engine = self._ensemble_engine()
        with self.assertRaises(ValueError):
            engine.generate_ensemble_voice(
                "hi", "", {"type": "ensemble", "members": ["Ghost"]}, {"Real": {}}, "x.wav")
        with self.assertRaises(ValueError):
            engine.generate_ensemble_voice(
                "hi", "", {"type": "ensemble", "members": []}, {}, "x.wav")

    def test_generate_batch_routes_ensemble_to_sequential_path(self):
        engine = self._ensemble_engine()
        voice_config = {
            "Duo": {"type": "ensemble", "members": ["X"]},
            "X": {"type": "custom"},
        }
        chunks = [{"index": 0, "text": "hi", "instruct": "", "speaker": "Duo"}]
        # generate_batch clears the GPU cache after each bucket, which imports
        # torch. Production always has it (install.js declares bundle "ai"), but
        # CI deliberately installs without it, so stand torch in here.
        with patch.object(engine, "_sequential_ensemble",
                          return_value={"completed": [0], "failed": []}) as seq, \
                patch.dict(sys.modules, {"torch": self._fake_torch(8 * 10**9)}):
            results = engine.generate_batch(chunks, voice_config, "/tmp")
        seq.assert_called_once()
        self.assertEqual(results["completed"], [0])

    def test_tts_engine_reads_configured_max_new_tokens(self):
        self.assertEqual(
            tts_module.TTSEngine({"tts": {"max_new_tokens": 4096}})._max_new_tokens, 4096)
        self.assertEqual(tts_module.TTSEngine({"tts": {}})._max_new_tokens, 2048)

    def test_tts_custom_warmup_retries_failure_and_runs_once_after_success(self):
        engine = tts_module.TTSEngine({"tts": {}})
        failed_model = SimpleNamespace(generate_custom_voice=Mock(
            side_effect=RuntimeError("warmup failed")))
        self.assertFalse(engine.ensure_custom_warmup(failed_model))
        self.assertTrue(engine._custom_warmup_needed)

        working_model = SimpleNamespace(generate_custom_voice=Mock())
        self.assertTrue(engine.ensure_custom_warmup(working_model))
        self.assertTrue(engine.ensure_custom_warmup(working_model))
        working_model.generate_custom_voice.assert_called_once()
        self.assertFalse(engine._custom_warmup_needed)

    def test_tts_benchmark_builds_nested_engine_config_without_mutation(self):
        original = {"language": "Spanish", "max_new_tokens": 4096}
        result = tts_benchmark_module.get_benchmark_engine_config(original)

        self.assertEqual({"language": "Spanish", "max_new_tokens": 4096}, original)
        self.assertEqual("local", result["tts"]["mode"])
        self.assertEqual("Spanish", result["tts"]["language"])
        self.assertEqual(4096, result["tts"]["max_new_tokens"])

    def _fake_torch(self, free_bytes):
        """Minimal torch stand-in, so the estimate is exercised without a GPU:
        _estimate_max_batch_size imports torch inside the function, so injecting
        sys.modules is enough to control mem_get_info."""
        torch = SimpleNamespace()
        torch.cuda = SimpleNamespace(
            is_available=lambda: True,
            mem_get_info=lambda: (free_bytes, free_bytes),
            memory_reserved=lambda: 0,
            memory_allocated=lambda: 0,
            empty_cache=lambda: None,
        )
        return torch

    def _max_batch_for(self, max_new_tokens):
        engine = tts_module.TTSEngine({"tts": {"max_new_tokens": max_new_tokens}})
        model = SimpleNamespace(model=SimpleNamespace(talker=SimpleNamespace(
            config=SimpleNamespace(num_hidden_layers=24, num_key_value_heads=4,
                                   hidden_size=1024, num_attention_heads=16))))
        with patch.dict(sys.modules, {"torch": self._fake_torch(8 * 10**9)}):
            return engine._estimate_max_batch_size(model, max_text_chars=300)

    def test_batch_estimate_uses_configured_max_new_tokens(self):
        # The estimator and the generate calls must read ONE value. If the
        # estimator kept assuming 2048 while generation used a larger cap, it
        # would under-count VRAM per sequence and over-size the batch -> OOM.
        low = self._max_batch_for(2048)
        high = self._max_batch_for(8192)
        self.assertGreater(low, high,
                           "a larger max_new_tokens must shrink the estimated batch")

    def test_tts_has_no_hardcoded_max_new_tokens(self):
        # Two independent 2048 literals (estimator default + generate calls) are
        # exactly the drift this consolidation removes; keep them from returning.
        source = Path(__file__).resolve().parent.joinpath("tts.py").read_text(encoding="utf-8")
        # assertFalse, not assertNotIn: the latter dumps all of tts.py on failure.
        self.assertFalse("max_new_tokens=2048" in source,
                         "tts.py must read max_new_tokens from config, not hardcode it")

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
            cfg = dict(core_module.VOICELAB_DEFAULTS,
                       zips_dir=tmp, rocm_python="")
            report = voicelab_module._build_voicelab_preflight(
                voicelab_module.VoiceLabRequest(stages=["train"]), cfg)
        self.assertIn("interpreter_missing",
                      [finding["code"] for finding in report["blockers"]])

    def test_voicelab_preflight_is_read_only_sanitized_and_stage_specific(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(voicelab_module, "_probe_voicelab_interpreter",
                          return_value={"python": "3.10", "torch": "2.7+rocm", "hip": "6.3",
                                        "gpu": "AMD Test", "vram": [12 * 1024**3, 16 * 1024**3],
                                        "deps": {"librosa": True, "peft": True}}):
            os.makedirs(os.path.join(tmp, "_deduped"))
            Path(tmp, "_deduped", "voice.zip").write_bytes(b"zip")
            cfg = dict(core_module.VOICELAB_DEFAULTS,
                       zips_dir=tmp, rocm_python=sys.executable)
            report = voicelab_module._build_voicelab_preflight(
                voicelab_module.VoiceLabRequest(stages=["train"]), cfg)
        self.assertTrue(report["ready"])
        self.assertEqual(1, report["dataset"]["deduped_count"])
        self.assertNotIn(tmp, json.dumps({key: value for key, value in report.items()
                                         if not key.startswith("_")}))

    def test_voicelab_start_rejects_stale_preflight_before_gpu_claim(self):
        request = voicelab_module.VoiceLabRequest(
            stages=["train"], preflight_id="old")
        report = {"preflight_id": "new", "blockers": [], "_zips_dir": "/zips",
                  "_profiler_model": ""}
        with patch.object(voicelab_module, "_build_voicelab_preflight", return_value=report), \
             patch.object(voicelab_module, "claim_gpu_task") as claim:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(voicelab_module.voicelab_start(request, None))
        self.assertEqual(409, raised.exception.status_code)
        claim.assert_not_called()

    def test_voicelab_request_rejects_unbounded_training_values(self):
        for values in ({"target_loss": 0}, {"target_loss": 101},
                       {"max_epochs": 0}, {"max_epochs": 101},
                       {"lora_r": 0}, {"lora_r": 1025},
                       {"device": "vulkan"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                voicelab_module.VoiceLabRequest(**values)

    def test_device_normalization_canonicalizes_amd_and_preserves_cuda_index(self):
        self.assertEqual("cuda", device_utils.normalize_device(" ROCm "))
        self.assertEqual("cuda", device_utils.normalize_device("hip"))
        self.assertEqual("cuda:2", device_utils.normalize_device("CUDA:2"))
        self.assertEqual("cuda", voicelab_module.VoiceLabRequest(
            stages=["train"], device=" ROCm ").device)

    def test_voicelab_passes_canonical_device_to_all_device_aware_stages(self):
        request = voicelab_module.VoiceLabRequest(
            stages=["dedup", "train", "evaluate"], device="hip")
        cfg = {"rocm_python": "/python", "profiler_model": "",
               "epub_dirs": []}

        steps = voicelab_module._voicelab_build_commands(request, cfg, "/zips")

        for stage, command, _cwd, _env in steps:
            with self.subTest(stage=stage):
                index = command.index("--device")
                self.assertEqual("cuda", command[index + 1])

    def test_voicelab_config_ignores_invalid_stored_shapes(self):
        defaults = {"rocm_python": "python", "profiler_model": "model",
                    "epub_dirs": ["books"], "zips_dir": "zips"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            Path(path).write_text(json.dumps({"rocm_python": 7,
                                              "profiler_model": [],
                                              "epub_dirs": ["valid", 9],
                                              "zips_dir": {}}), encoding="utf-8")
            with patch.object(core_module, "VOICELAB_CONFIG_PATH", path), \
                 patch.object(core_module, "VOICELAB_DEFAULTS", defaults):
                loaded = core_module._load_voicelab_config()

        self.assertEqual(defaults, loaded)
        self.assertIsNot(defaults["epub_dirs"], loaded["epub_dirs"])

    def test_voicelab_preflight_is_dispatched_off_event_loop(self):
        calls = []

        async def fake_to_thread(function, *args):
            calls.append((function, args))
            return {}

        cfg = {"rocm_python": sys.executable, "profiler_model": "",
               "epub_dirs": [], "zips_dir": ""}
        with patch.object(voicelab_module, "_load_voicelab_config", return_value=cfg), \
             patch.object(voicelab_module.asyncio, "to_thread", side_effect=fake_to_thread):
            result = asyncio.run(voicelab_module.voicelab_get_config())

        self.assertEqual(1, len(calls))
        self.assertIs(voicelab_module._run_profiler_preflight, calls[0][0])
        self.assertTrue(result["checks"]["profiler_environment"])

    def test_voicelab_runtime_revalidation_detects_missing_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = voicelab_module._revalidate_voicelab_runtime(
                tmp, os.path.join(tmp, "missing-python"), "", ["train"])
        self.assertIsNotNone(error)
        self.assertIn("not found or not executable", error.detail)

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
            Path(tmp, "volume-1.json.generation_checkpoint.json").write_text("{}", encoding="utf-8")
            Path(tmp, "volume-1.json.generation_quality.json").write_text("{}", encoding="utf-8")
            listed = asyncio.run(scripts_library_module.list_saved_scripts())
        self.assertEqual([item["name"] for item in listed], ["volume-1"])

    def test_delete_saved_script_removes_generation_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "book.json")
            quality = Path(str(script) + ".generation_quality.json")
            generation_checkpoint = Path(str(script) + ".generation_checkpoint.json")
            script.write_text("[]", encoding="utf-8")
            quality.write_text("{}", encoding="utf-8")
            generation_checkpoint.write_text("{}", encoding="utf-8")
            with patch.object(scripts_library_module, "SCRIPTS_DIR", tmp), \
                 patch.object(scripts_library_module, "_saved_book_meta_path",
                              return_value=str(Path(tmp, "book.meta.json"))), \
                 patch.object(scripts_library_module, "_checkpoint_path",
                              return_value=str(Path(tmp, "book.review_checkpoint.json"))):
                asyncio.run(scripts_library_module.delete_script("book"))
            self.assertFalse(script.exists())
            self.assertFalse(quality.exists())
            self.assertFalse(generation_checkpoint.exists())

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
