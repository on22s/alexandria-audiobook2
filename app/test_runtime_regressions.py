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
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app as app_module
import core as core_module
import tts as tts_module
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
        for fname in ("batch_train_lora.py", "voice_profiler.py",
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
            exc = self._voicelab_start_with({"zips_dir": tmp, "rocm_python": ""})
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
