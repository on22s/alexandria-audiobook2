"""Backend regression tests for DeepSeek full v3 systemic repairs (#1729)."""
import hashlib
import json
import os
import shutil
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
from unittest.mock import patch

import numpy as np
import soundfile as sf


class ReviewRepairsTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voice_design
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.root = root
        self.uploads = root / "uploads"
        self.uploads.mkdir()
        self.voices = root / "voices"
        self.voices.mkdir()
        self.manifest_path = self.voices / "manifest.json"
        for name, value in [("UPLOADS_DIR", str(self.uploads)), ("CLONE_VOICES_DIR", str(self.voices)), ("CLONE_VOICES_MANIFEST", str(self.manifest_path))]:
            self.stack.enter_context(patch.object(voice_design, name, value))
        self.disk = self.stack.enter_context(patch.object(voice_design, "check_disk_space", return_value=(True, 20)))
        api = FastAPI()
        api.include_router(voice_design.router)
        self.client = self.stack.enter_context(TestClient(api, raise_server_exceptions=False))
        self.vd = voice_design

    def _stage(self, filename="source.wav", peak=0.2, seconds=5):
        rate = 24000
        sample_times = np.arange(rate * seconds) / rate
        samples = (peak * np.sin(2 * np.pi * 220 * sample_times) * 32767).astype(np.int16)
        source = self.root / ("src-" + filename)
        sf.write(source, samples, rate, subtype="PCM_16")
        staged = self.client.post("/api/audio_channels/stage", data={"purpose": "clone"}, files={"file": (filename, source.read_bytes(), "audio/wav")})
        self.assertEqual(200, staged.status_code, staged.text)
        return staged.json()["token"]

    def _prepare(self, token, gain="1.0"):
        return self.client.post("/api/clone_voices/cleanup/prepare", data={
            "preview_token": token, "audio_channel": "mix", "gain": gain, "remove_dc": "false",
            "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true"})

    def _accept(self, body):
        return self.client.post("/api/clone_voices/cleanup/accept", data={
            "cleanup_token": body["cleanup_token"], "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true",
            "listened_original": "true", "listened_candidate": "true", "raw_source_sha256": body["raw_source_sha256"],
            "normalized_original_sha256": body["normalized_original_sha256"], "candidate_sha256": body["candidate_sha256"],
            "plan_digest_value": body["plan_digest"], "provenance_digest_value": body["provenance_digest"], "audio_channel": "mix"})

    def _prepared_body(self, filename="source.wav"):
        prepared = self._prepare(self._stage(filename=filename))
        self.assertEqual(200, prepared.status_code, prepared.text)
        return prepared.json()

    def _cleanup_root(self):
        return self.uploads / ".reference-cleanup"

    def test_r03_old_sessionless_and_corrupt_dirs_swept_freeing_cap(self):
        root = self._cleanup_root()
        root.mkdir(parents=True, exist_ok=True)
        old = time.time() - 2000
        for index in range(8):
            directory = root / (f"{index:032x}")
            directory.mkdir()
            if index % 2:
                (directory / "session.json").write_text("{not json", encoding="utf-8")
            os.utime(directory, (old, old))
        young = root / ("f" * 32)
        young.mkdir()
        (young / "session.json").write_text("{bad", encoding="utf-8")
        prepared = self._prepare(self._stage())
        self.assertEqual(200, prepared.status_code, prepared.text)
        prepared_token = prepared.json()["cleanup_token"]
        self.assertTrue(young.is_dir())
        remaining_old = [directory for directory in root.iterdir() if directory.is_dir() and directory != young and directory.name != prepared_token and len(directory.name) == 32]
        self.assertEqual([], remaining_old)

    def test_r03_sweep_respects_active_token_lock(self):
        root = self._cleanup_root()
        root.mkdir(parents=True, exist_ok=True)
        token = "a" * 32
        directory = root / token
        directory.mkdir()
        old = time.time() - 2000
        os.utime(directory, (old, old))
        with self.vd._cleanup_lock(token):
            self.vd._sweep_expired_cleanup_sessions()
            self.assertTrue(directory.is_dir())
        self.vd._sweep_expired_cleanup_sessions()
        self.assertFalse(directory.is_dir())

    def test_r02_corrupt_audio_prepare_is_400_without_residue(self):
        staged = self.client.post("/api/audio_channels/stage", data={"purpose": "clone"}, files={"file": ("corrupt.wav", b"not audio at all", "audio/wav")})
        self.assertEqual(200, staged.status_code, staged.text)
        prepared = self._prepare(staged.json()["token"])
        self.assertEqual(400, prepared.status_code, prepared.text)
        root = self._cleanup_root()
        leftovers = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
        self.assertEqual([], leftovers)
        self.assertFalse(self.manifest_path.exists())

    def test_r12_prepare_cancelled_at_commit_leaves_no_session(self):
        token = self._stage()
        original_save = self.vd._save_cleanup_session
        cancelled = threading.Event()
        def save_then_cancel(directory, session):
            original_save(directory, session)
            cancelled.set()
        with patch.object(self.vd, "_save_cleanup_session", side_effect=save_then_cancel):
            plan = __import__("reference_cleanup").validate_cleanup_plan(1.0, False)
            provenance = {"ref_text": "Synthetic reference transcript.", "source_title": "", "source_url": "", "rights_basis": "", "rights_confirmed": True}
            with self.assertRaises(ValueError):
                self.vd._prepare_cleanup_sync(token, "mix", plan, provenance, cancelled)
        root = self._cleanup_root()
        leftovers = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
        self.assertEqual([], leftovers)

    def test_r12_accept_cancelled_inside_manifest_lock_does_not_publish(self):
        body = self._prepared_body()
        echoes = (body["raw_source_sha256"], body["normalized_original_sha256"], body["candidate_sha256"], body["plan_digest"], body["provenance_digest"], "mix")
        provenance = body["provenance"]
        cancelled = threading.Event()
        outcome = {}
        reached_manifest_lock = threading.Event()
        original_lock = self.vd.file_lock
        @contextmanager
        def observed_lock(path, *arguments, **keywords):
            if str(path) == str(self.manifest_path):
                reached_manifest_lock.set()
            with original_lock(path, *arguments, **keywords):
                yield
        def run():
            try:
                outcome["result"] = self.vd._accept_cleanup_sync(body["cleanup_token"], provenance, body["provenance_digest"], echoes, cancelled)
            except BaseException as error:
                outcome["error"] = error
        with patch.object(self.vd, "file_lock", side_effect=observed_lock):
            with original_lock(str(self.manifest_path)):
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(reached_manifest_lock.wait(5), "accept must reach the held manifest lock")
                cancelled.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("error", outcome, outcome)
        self.assertFalse(self.manifest_path.exists())
        self.assertEqual([], [p for p in self.voices.iterdir() if p.suffix == ".wav"])

    def test_cancel_after_voice_staging_before_manifest_save_rolls_back(self):
        body = self._prepared_body()
        cancelled = threading.Event()
        original_replace = os.replace
        def replace_then_cancel(source, destination):
            original_replace(source, destination)
            if Path(destination).parent == self.voices:
                cancelled.set()
        echoes = (body["raw_source_sha256"], body["normalized_original_sha256"], body["candidate_sha256"], body["plan_digest"], body["provenance_digest"], "mix")
        with patch.object(self.vd.os, "replace", side_effect=replace_then_cancel):
            with self.assertRaises(ValueError):
                self.vd._accept_cleanup_sync(body["cleanup_token"], body["provenance"], body["provenance_digest"], echoes, cancelled)
        self.assertFalse(self.manifest_path.exists())
        self.assertEqual([], [path for path in self.voices.iterdir() if path.suffix == ".wav"])
        self.assertEqual([], list((self.uploads / ".reference-cleanup-lineage").iterdir()))

    def test_r04_delete_removes_deterministic_lineage_and_surfaces_errors(self):
        body = self._prepared_body()
        accepted = self._accept(body)
        self.assertEqual(200, accepted.status_code, accepted.text)
        voice_id = accepted.json()["voice_id"]
        lineage = self.uploads / ".reference-cleanup-lineage" / voice_id
        self.assertTrue(lineage.is_dir())
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        manifest = json.loads(self.manifest_path.read_text())
        manifest[0]["cleanup_receipt"]["lineage"]["raw_source"] = str(outside)
        self.manifest_path.write_text(json.dumps(manifest))
        deleted = self.client.delete(f"/api/clone_voices/{voice_id}")
        self.assertEqual(200, deleted.status_code, deleted.text)
        self.assertFalse(lineage.exists())
        self.assertTrue(outside.exists())
        body2 = self._prepared_body()
        accepted2 = self._accept(body2)
        self.assertEqual(200, accepted2.status_code, accepted2.text)
        voice_id2 = accepted2.json()["voice_id"]
        lineage2 = self.uploads / ".reference-cleanup-lineage" / voice_id2
        shutil.rmtree(lineage2)
        lineage2.write_text("not a dir", encoding="utf-8")
        failed = self.client.delete(f"/api/clone_voices/{voice_id2}")
        self.assertEqual(500, failed.status_code, failed.text)
        remaining = json.loads(self.manifest_path.read_text())
        self.assertEqual([voice_id2], [entry["id"] for entry in remaining])
        self.assertTrue((self.voices / accepted2.json()["filename"]).is_file())
        lineage2.unlink()
        retried = self.client.delete(f"/api/clone_voices/{voice_id2}")
        self.assertEqual(200, retried.status_code, retried.text)

    def test_r08_strict_manifest_read_fails_closed(self):
        body = self._prepared_body()
        self.manifest_path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.vd._find_cleanup_publication("x" * 32)
        result = self._accept(body)
        self.assertNotEqual(200, result.status_code, result.text)
        self.assertEqual("{corrupt", self.manifest_path.read_text())
        self.assertEqual([], [p for p in self.voices.iterdir() if p.suffix == ".wav"])

    def test_r05_token_lock_timeout_maps_to_503(self):
        body = self._prepared_body()
        with patch.object(self.vd, "file_lock", side_effect=TimeoutError("locked")):
            audio = self.client.get(f"/api/clone_voices/cleanup/{body['cleanup_token']}/audio", params={"which": "original"})
            self.assertEqual(503, audio.status_code, audio.text)
            accept = self._accept(body)
            self.assertEqual(503, accept.status_code, accept.text)

    def test_r06_slot_exhaustion_maps_to_503_not_candidate_refused(self):
        import reference_cleanup
        body = self._prepared_body()
        self.assertTrue(reference_cleanup._SLOTS.acquire(blocking=False))
        self.assertTrue(reference_cleanup._SLOTS.acquire(blocking=False))
        try:
            accept = self._accept(body)
            self.assertEqual(503, accept.status_code, accept.text)
            self.assertNotIn("Candidate refused", accept.text)
            prepared = self._prepare(self._stage())
            self.assertEqual(503, prepared.status_code, prepared.text)
        finally:
            reference_cleanup._SLOTS.release()
            reference_cleanup._SLOTS.release()
        retry = self._accept(body)
        self.assertEqual(200, retry.status_code, retry.text)

    def test_r09_missing_cleanup_root_is_400_without_recreating(self):
        token = "b" * 32
        root = self._cleanup_root()
        self.assertFalse(root.exists())
        audio = self.client.get(f"/api/clone_voices/cleanup/{token}/audio", params={"which": "original"})
        self.assertEqual(400, audio.status_code, audio.text)
        self.assertFalse(root.exists())
        accept = self.client.post("/api/clone_voices/cleanup/accept", data={
            "cleanup_token": token, "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true",
            "listened_original": "true", "listened_candidate": "true", "raw_source_sha256": "0" * 64,
            "normalized_original_sha256": "0" * 64, "candidate_sha256": "0" * 64,
            "plan_digest_value": "0" * 64, "provenance_digest_value": "0" * 64, "audio_channel": "mix"})
        self.assertEqual(400, accept.status_code, accept.text)
        self.assertFalse(root.exists())

    def test_r10_r39_disk_wording_and_accept_preflight(self):
        token1 = self._stage()
        token2 = self._stage()
        self.disk.return_value = (False, 0.0)
        result = self._prepare(token1)
        self.assertEqual(400, result.status_code)
        self.assertIn("unavailable", result.json()["detail"])
        self.disk.return_value = (False, 1.0)
        result = self._prepare(token2)
        self.assertEqual(400, result.status_code)
        self.assertIn("At least 2 GB", result.json()["detail"])
        self.disk.return_value = (True, 20)
        body = self._prepared_body()
        self.disk.return_value = (False, 1.0)
        accept = self._accept(body)
        self.assertEqual(400, accept.status_code, accept.text)
        self.assertIn("At least 2 GB", accept.json()["detail"])
        self.assertFalse(self.manifest_path.exists())

    def test_r19_long_stem_preserves_cleaned_suffix(self):
        body = self._prepared_body(filename="a" * 100 + ".wav")
        accepted = self._accept(body)
        self.assertEqual(200, accepted.status_code, accepted.text)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertTrue(manifest[0]["name"].endswith("-cleaned"), manifest[0]["name"])
        self.assertLessEqual(len(manifest[0]["name"]), 80)

    def test_r25_lineage_copy_corruption_refuses_publication(self):
        body = self._prepared_body()
        original_copy = shutil.copyfile
        def corrupting_copy(src, dst):
            original_copy(src, dst)
            if "lineage" in str(dst):
                data = Path(dst).read_bytes()
                Path(dst).write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])
        with patch.object(shutil, "copyfile", side_effect=corrupting_copy):
            accept = self._accept(body)
        self.assertEqual(400, accept.status_code, accept.text)
        self.assertFalse(self.manifest_path.exists())
        lineage_base = self.uploads / ".reference-cleanup-lineage"
        leftovers = list(lineage_base.iterdir()) if lineage_base.is_dir() else []
        self.assertEqual([], leftovers)

    def test_accept_publication_copy_stays_inside_cleanup_slot(self):
        import reference_cleanup
        body = self._prepared_body()
        original_copy = shutil.copyfile
        depths = []
        def observed_copy(source, destination):
            depths.append(getattr(reference_cleanup._LOCAL, "depth", 0))
            return original_copy(source, destination)
        with patch.object(self.vd.shutil, "copyfile", side_effect=observed_copy):
            result = self._accept(body)
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(3, len(depths))
        self.assertTrue(all(depth > 0 for depth in depths), depths)

    def test_r27_audio_hashes_returned_bytes(self):
        body = self._prepared_body()
        with patch.object(self.vd, "sha256_file", return_value="0" * 64):
            audio = self.client.get(f"/api/clone_voices/cleanup/{body['cleanup_token']}/audio", params={"which": "candidate"})
        self.assertEqual(200, audio.status_code, audio.text)
        self.assertEqual(body["candidate_sha256"], hashlib.sha256(audio.content).hexdigest())

    def test_r35_sha256_file_is_cancellation_aware_and_local(self):
        target = self.root / "blob.bin"
        target.write_bytes(b"x" * (2 * 1024 * 1024))
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(ValueError):
            self.vd.sha256_file(str(target), cancelled)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), self.vd.sha256_file(str(target), threading.Event()))
        self.assertNotIn("audio_mastering", Path(self.vd.__file__).read_text())

    def test_r07_discard_contract_is_truthful_and_idempotent(self):
        body = self._prepared_body()
        accepted = self._accept(body)
        self.assertEqual(200, accepted.status_code, accepted.text)
        discarded = self.client.delete(f"/api/clone_voices/cleanup/{body['cleanup_token']}")
        self.assertEqual(200, discarded.status_code, discarded.text)
        payload = discarded.json()
        self.assertEqual("discarded", payload["status"])
        self.assertEqual("Preview removed if present; already accepted voices remain in the library.", payload["detail"])
        self.assertEqual(1, len(json.loads(self.manifest_path.read_text())))
        missing = self.client.delete(f"/api/clone_voices/cleanup/{'c' * 32}")
        self.assertEqual(200, missing.status_code, missing.text)
        self.assertEqual("discarded", missing.json()["status"])

    def test_shared_provenance_caps_refuse_before_plain_or_cleanup_publication(self):
        token = self._stage()
        fields = {"preview_token": token, "ref_text": "Synthetic transcript.", "rights_confirmed": "true"}
        limits = {"ref_text": 4000, "source_title": 512, "source_url": 2048, "rights_basis": 1024}
        html = (Path(self.vd.__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        for field, limit in limits.items():
            for route in ("/api/clone_voices/upload", "/api/clone_voices/cleanup/prepare"):
                with self.subTest(field=field, route=route):
                    result = self.client.post(route, data={**fields, field: "x" * (limit + 1)})
                    self.assertEqual(400, result.status_code, result.text)
                    self.assertIn(str(limit), result.json()["detail"])
                    self.assertFalse(self.manifest_path.exists())
            control_id = "clone-import-" + field.replace("_", "-")
            self.assertIn(f'id="{control_id}" maxlength="{limit}"', html)


if __name__ == "__main__":
    unittest.main()
