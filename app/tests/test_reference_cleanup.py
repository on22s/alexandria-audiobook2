"""Affine cleanup instrument controls and route behaviour."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from reference_cleanup import apply_affine_cleanup, validate_cleanup_plan, verify_affine_candidate


class InstrumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rate = 24000
        t = np.arange(self.rate * 5) / self.rate
        self.samples = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        self.original = self.root / "original.wav"
        sf.write(self.original, self.samples, self.rate, subtype="PCM_16")
        self.plan = validate_cleanup_plan(1.5, True)

    def test_positive_identity_and_gain(self):
        candidate = self.root / "candidate.wav"
        apply_affine_cleanup(str(self.original), str(candidate), validate_cleanup_plan(1.0, False))
        valid, problems, records = verify_affine_candidate(str(self.original), str(candidate), validate_cleanup_plan(1.0, False))
        self.assertTrue(valid, problems)
        self.assertEqual("UNMEASURED", records["asr"])
        gain_plan = validate_cleanup_plan(1.5, True)
        apply_affine_cleanup(str(self.original), str(candidate), gain_plan)
        valid, problems, _ = verify_affine_candidate(str(self.original), str(candidate), gain_plan)
        self.assertTrue(valid, problems)

    def test_clipping_fails(self):
        loud = (0.9 * np.sin(2 * np.pi * 220 * np.arange(self.rate * 5) / self.rate) * 32767).astype(np.int16)
        loud_path = self.root / "loud.wav"
        sf.write(loud_path, loud, self.rate, subtype="PCM_16")
        with self.assertRaises(ValueError):
            apply_affine_cleanup(str(loud_path), str(self.root / "clipped.wav"), validate_cleanup_plan(2.0, False))

    def test_apply_and_verify_refuse_noncanonical_plans(self):
        candidate = self.root / "candidate.wav"
        apply_affine_cleanup(str(self.original), str(candidate), self.plan)
        altered_plans = [
            {**self.plan, "extra": True},
            {**self.plan, "operations": list(reversed(self.plan["operations"]))},
            {**self.plan, "selected_bounds": [0, 5]},
            {**self.plan, "operations": self.plan["operations"] + [{"op": "denoise"}]},
            {**self.plan, "operations": [{"op": "dc_offset_removal", "enabled": "false"}, {"op": "gain", "value": 1.5}]},
        ]
        before = candidate.read_bytes()
        for plan in altered_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    apply_affine_cleanup(str(self.original), str(candidate), plan)
                valid, problems, records = verify_affine_candidate(str(self.original), str(candidate), plan)
                self.assertFalse(valid)
                self.assertTrue(problems)
                self.assertEqual("UNMEASURED", records["asr"])
                self.assertEqual(before, candidate.read_bytes())

    def test_removed_span_fails(self):
        candidate = self.root / "candidate.wav"
        apply_affine_cleanup(str(self.original), str(candidate), self.plan)
        data, rate = sf.read(candidate, dtype="int16")
        sf.write(candidate, np.concatenate((data[:1000], data[2000:])), rate, subtype="PCM_16")
        valid, problems, _ = verify_affine_candidate(str(self.original), str(candidate), self.plan)
        self.assertFalse(valid)
        self.assertTrue(any("frame count" in p for p in problems))

    def test_substituted_equal_duration_energy_fails(self):
        candidate = self.root / "candidate.wav"
        apply_affine_cleanup(str(self.original), str(candidate), self.plan)
        data, rate = sf.read(candidate, dtype="int16")
        replacement = (data * -1).astype(np.int16)
        sf.write(candidate, replacement, rate, subtype="PCM_16")
        valid, problems, _ = verify_affine_candidate(str(self.original), str(candidate), self.plan)
        self.assertFalse(valid)
        self.assertTrue(any("mismatch" in p for p in problems))

    def test_wrong_plan_fails(self):
        candidate = self.root / "candidate.wav"
        apply_affine_cleanup(str(self.original), str(candidate), self.plan)
        valid, problems, _ = verify_affine_candidate(str(self.original), str(candidate), validate_cleanup_plan(1.0, False))
        self.assertFalse(valid)


class CleanupRouteTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
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
        self.stack.enter_context(patch.object(voice_design, "check_disk_space", return_value=(True, 20)))
        api = FastAPI()
        api.include_router(voice_design.router)
        self.client = self.stack.enter_context(TestClient(api, raise_server_exceptions=False))

    def _stage(self, peak=0.2, seconds=5):
        rate = 24000
        t = np.arange(rate * seconds) / rate
        samples = (peak * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        source = self.root / "source.wav"
        sf.write(source, samples, rate, subtype="PCM_16")
        staged = self.client.post("/api/audio_channels/stage", data={"purpose": "clone"}, files={"file": ("source.wav", source.read_bytes(), "audio/wav")})
        self.assertEqual(200, staged.status_code, staged.text)
        return staged.json()["token"]

    def _prepare(self, token, gain="1.5"):
        return self.client.post("/api/clone_voices/cleanup/prepare", data={
            "preview_token": token, "audio_channel": "mix", "gain": gain, "remove_dc": "false",
            "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true"})

    def _accept(self, body):
        return self.client.post("/api/clone_voices/cleanup/accept", data={
            "cleanup_token": body["cleanup_token"], "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true",
            "listened_original": "true", "listened_candidate": "true", "raw_source_sha256": body["raw_source_sha256"],
            "normalized_original_sha256": body["normalized_original_sha256"], "candidate_sha256": body["candidate_sha256"],
            "plan_digest_value": body["plan_digest"], "provenance_digest_value": body["provenance_digest"], "audio_channel": "mix"})

    def test_prepare_accept_reject_and_no_mutation_before_accept(self):
        prepared = self._prepare(self._stage())
        self.assertEqual(200, prepared.status_code, prepared.text)
        self.assertEqual([], list(self.voices.iterdir()))
        body = prepared.json()
        self.assertEqual("verified", body["integrity"])
        self.assertIn("samples_checked", body["verification"])
        cleanup_token = body["cleanup_token"]
        original = self.client.get(f"/api/clone_voices/cleanup/{cleanup_token}/audio", params={"which": "original"})
        candidate = self.client.get(f"/api/clone_voices/cleanup/{cleanup_token}/audio", params={"which": "candidate"})
        self.assertEqual(200, original.status_code)
        self.assertEqual(200, candidate.status_code)
        accepted = self._accept(body)
        self.assertEqual(200, accepted.status_code, accepted.text)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(1, len(manifest))
        self.assertEqual("affine-cleanup", manifest[0]["cleanup_receipt"]["origin"])
        self.assertTrue(manifest[0]["name"].endswith("-cleaned"))
        self.assertIsNotNone(manifest[0]["source_channels"])
        self.assertIn("lineage", manifest[0]["cleanup_receipt"])
        repeat = self._accept(body)
        self.assertEqual(400, repeat.status_code)
        self.assertEqual(1, len(json.loads(self.manifest_path.read_text())))

    def test_reject_removes_session_and_blocks_accept(self):
        prepared = self._prepare(self._stage())
        self.assertEqual(200, prepared.status_code, prepared.text)
        body = prepared.json()
        rejected = self.client.delete(f"/api/clone_voices/cleanup/{body['cleanup_token']}")
        self.assertEqual(200, rejected.status_code, rejected.text)
        self.assertEqual(400, self._accept(body).status_code)
        self.assertFalse(self.manifest_path.exists())

    def test_refused_original_never_reaches_candidate(self):
        short = self.root / "short.wav"
        sf.write(short, np.zeros(2400, dtype=np.int16), 24000, subtype="PCM_16")
        staged = self.client.post("/api/audio_channels/stage", data={"purpose": "clone"}, files={"file": ("short.wav", short.read_bytes(), "audio/wav")})
        prepared = self._prepare(staged.json()["token"], gain="1.0")
        self.assertEqual(400, prepared.status_code, prepared.text)
        self.assertEqual([], list(self.voices.iterdir()))

    def test_stale_echo_refused(self):
        prepared = self._prepare(self._stage(), gain="1.0")
        body = prepared.json()
        self.assertEqual(200, prepared.status_code, prepared.text)
        stale = self.client.post("/api/clone_voices/cleanup/accept", data={
            "cleanup_token": body["cleanup_token"], "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true",
            "listened_original": "true", "listened_candidate": "true", "raw_source_sha256": "0" * 64,
            "normalized_original_sha256": body["normalized_original_sha256"], "candidate_sha256": body["candidate_sha256"],
            "plan_digest_value": body["plan_digest"], "provenance_digest_value": body["provenance_digest"], "audio_channel": "mix"})
        self.assertEqual(400, stale.status_code)
        self.assertFalse(self.manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
