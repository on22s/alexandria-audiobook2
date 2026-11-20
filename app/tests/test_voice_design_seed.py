"""Designer preview seed plumbing and server-owned preview provenance.

Fail-first coverage for #2140. The HTTP boundary used to accept `seed` through
Pydantic's extra="ignore", forward the engine default (-1), and answer 200. A
first fix forwarded the caller's seed but let the save route trust an unrelated
caller-supplied seed and ignore `language`. These tests drive the REAL schemas,
FastAPI routes and manifest writer; only the engine is stubbed. No GPU, no
billable inference, no user data.
"""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import core
from routers import voice_design


SEED = 733
OTHER_SEED = 734
MAX_SEED = 2**64 - 1


def _write_tone(path, seed):
    """A tiny deterministic WAV; a pinned seed must reproduce these bytes."""
    import wave
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        value = (seed if seed >= 0 else 32767) & 0x7FFF
        handle.writeframes(b"".join(struct.pack("<h", value) for _ in range(8)))


class RecordingEngine:
    def __init__(self, directory, language="English"):
        self.directory = Path(directory)
        self._language = language
        self.calls = []
        self.outputs = []

    def generate_voice_design(self, description, sample_text, language=None, seed=-1, **kwargs):
        self.calls.append({"description": description, "sample_text": sample_text,
                           "language": language, "seed": seed, "kwargs": kwargs})
        output = self.directory / f"preview_{len(self.calls)}.wav"
        _write_tone(output, seed)
        self.outputs.append(output)
        return str(output), 24000


class VoiceDesignSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previews = self.root / "previews"
        self.manifest = self.root / "manifest.json"
        self.production_data_dir = core.DATA_DIR
        self.production_designed_dir = voice_design.DESIGNED_VOICES_DIR
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values():
            row["running"] = False
        for name, value in (("process_state", self.state),
                            ("_task_claims", {}),
                            ("_gpu_leases", {}),
                            ("DATA_DIR", str(self.root)),
                            ("acquire_gpu_lock", lambda *a, **k: None),
                            ("check_global_gpu_lock", lambda *a, **k: None),
                            ("llm_is_on_this_gpu", lambda *a, **k: True)):
            ctx = patch.object(core, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        for name, value in (("DESIGNED_VOICES_DIR", str(self.root)),
                            ("DESIGNED_VOICES_MANIFEST", str(self.manifest))):
            ctx = patch.object(voice_design, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        self.engine = RecordingEngine(self.previews)

    def _app(self):
        app = FastAPI()
        app.include_router(voice_design.router)
        ctx = patch.object(voice_design.project_manager, "get_engine", return_value=self.engine)
        ctx.start()
        self.addCleanup(ctx.stop)
        return app

    def _post(self, path, payload):
        with TestClient(self._app(), raise_server_exceptions=False) as client:
            return client.post(path, json=payload)

    def _preview(self, seed=None, language=None):
        payload = {"description": "A synthetic narrator", "sample_text": "Synthetic sample line."}
        if seed is not None:
            payload["seed"] = seed
        if language is not None:
            payload["language"] = language
        response = self._post("/api/voice_design/preview", payload)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _save(self, preview_file, **overrides):
        payload = {"name": "Synthetic voice", "description": "A synthetic narrator",
                   "sample_text": "Synthetic sample line.", "preview_file": preview_file}
        payload.update(overrides)
        response = self._post("/api/voice_design/save", payload)
        return response

    def _entries(self):
        if not self.manifest.exists():
            return []
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    # --- schema / range boundary ---------------------------------------------

    def test_request_schema_accepts_seed_and_defaults_to_random(self):
        pinned = voice_design.VoiceDesignPreviewRequest(
            description="d", sample_text="s", seed=SEED)
        self.assertEqual(SEED, pinned.model_dump()["seed"])
        omitted = voice_design.VoiceDesignPreviewRequest(description="d", sample_text="s")
        self.assertEqual(-1, omitted.model_dump()["seed"])

    def test_request_schema_rejects_an_unsupported_negative_seed(self):
        with self.assertRaises(ValidationError):
            voice_design.VoiceDesignPreviewRequest(description="d", sample_text="s", seed=-2)

    def test_http_range_boundaries_are_422_instead_of_a_500_at_the_rng(self):
        """-1 and [0, 2**64) are portable; 2**64 overflows the engine RNG."""
        for accepted in (-1, 0, MAX_SEED):
            with self.subTest(accepted=accepted):
                result = self._preview(seed=accepted)
                self.assertEqual("ok", result["status"])
        for rejected in (-2, 2**64):
            with self.subTest(rejected=rejected):
                response = self._post("/api/voice_design/preview",
                                      {"description": "d", "sample_text": "s", "seed": rejected})
                self.assertEqual(422, response.status_code, response.text)

    # --- preview -> engine ---------------------------------------------------

    def test_route_forwards_seed_and_omission_stays_random(self):
        self._preview(seed=SEED)
        self.assertEqual([SEED], [call["seed"] for call in self.engine.calls])
        self.engine = RecordingEngine(self.previews)
        self._preview()
        self.assertEqual([-1], [call["seed"] for call in self.engine.calls])

    def test_same_seed_reproduces_audio_and_a_different_seed_does_not(self):
        """Synthetic seeded engine: reproducibility checked without real synthesis."""
        first = self._preview(seed=SEED)
        second = self._preview(seed=SEED)
        third = self._preview(seed=OTHER_SEED)
        outputs = {path.name: path.read_bytes() for path in self.engine.outputs}
        pick = lambda result: outputs[result["audio_url"].rsplit("/", 1)[-1]]
        self.assertEqual(pick(first), pick(second))
        self.assertNotEqual(pick(first), pick(third))

    def test_seed_is_per_request_and_leaves_task_state_clean(self):
        self._preview(seed=11)
        self._preview(seed=22)
        self.assertEqual([11, 22], [call["seed"] for call in self.engine.calls])
        self.assertFalse(self.state["voice_design"]["running"])
        self.assertNotIn("voice_design", core._task_claims)

    # --- preview receipt -> save provenance ----------------------------------

    def test_save_derives_seed_and_effective_language_from_preview_receipt(self):
        result = self._preview(seed=SEED, language="English")
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        for supplied in (None, SEED):
            with self.subTest(supplied=supplied):
                overrides = {} if supplied is None else {"seed": supplied}
                response = self._save(preview_file, **overrides)
                self.assertEqual(200, response.status_code, response.text)
                entry = self._entries()[-1]
                self.assertEqual(SEED, entry["seed"])
                self.assertEqual("English", entry["language"])

    def test_save_effective_language_falls_back_to_engine_config(self):
        self.engine = RecordingEngine(self.previews, language="Chinese")
        result = self._preview(seed=SEED)
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        response = self._save(preview_file)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("Chinese", self._entries()[-1]["language"])

    def test_save_rejects_a_seed_that_disagrees_with_the_receipt(self):
        result = self._preview(seed=SEED, language="English")
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        response = self._save(preview_file, seed=OTHER_SEED)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual([], self._entries())

    def test_save_rejects_inputs_that_changed_since_the_preview(self):
        result = self._preview(seed=SEED, language="English")
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        for field in ("description", "sample_text"):
            with self.subTest(field=field):
                response = self._save(preview_file, **{field: "changed after preview"})
                self.assertEqual(400, response.status_code, response.text)

    def test_save_receipt_is_bound_to_the_actual_preview_bytes(self):
        result = self._preview(seed=SEED, language="English")
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        (self.previews / preview_file).write_bytes(b"tampered preview bytes")
        response = self._save(preview_file)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual([], self._entries())

    def test_concurrent_previews_keep_their_own_provenance(self):
        first = self._preview(seed=11, language="English")
        second = self._preview(seed=22, language="Chinese")
        self.assertEqual(200, self._save(first["audio_url"].rsplit("/", 1)[-1]).status_code)
        self.assertEqual(200, self._save(second["audio_url"].rsplit("/", 1)[-1]).status_code)
        entries = self._entries()
        self.assertEqual([11, 22], [entry["seed"] for entry in entries])
        self.assertEqual(["English", "Chinese"], [entry["language"] for entry in entries])

    # --- legacy / resave -----------------------------------------------------

    def test_legacy_preview_never_claims_an_unverified_seed(self):
        legacy = self.previews / "legacy.wav"
        _write_tone(legacy, -1)
        rejected = self._save("legacy.wav", seed=SEED)
        self.assertEqual(400, rejected.status_code, rejected.text)
        self.assertEqual([], self._entries())
        accepted = self._save("legacy.wav")
        self.assertEqual(200, accepted.status_code, accepted.text)
        entry = self._entries()[-1]
        self.assertNotIn("seed", entry)
        self.assertNotIn("language", entry)

    def test_receipts_are_stored_outside_the_web_served_asset_directory(self):
        self._preview(seed=SEED, language="English")
        receipts = Path(self.production_data_dir) / "design_preview_receipts"
        self.assertFalse(receipts.is_relative_to(Path(self.production_designed_dir)))

    def test_legacy_replacement_clears_stale_provenance_and_changes_bytes(self):
        """A receipt-less replacement must not inherit the old seed/language."""
        original = self._preview(seed=SEED, language="English")
        voice_id = self._save(original["audio_url"].rsplit("/", 1)[-1]).json()["voice_id"]
        entry = self._entries()[-1]
        dest = self.root / entry["filename"]
        old_bytes = dest.read_bytes()

        other = self._preview(seed=OTHER_SEED)
        legacy = self.previews / "legacy-replacement.wav"
        legacy.write_bytes((self.previews / other["audio_url"].rsplit("/", 1)[-1]).read_bytes())
        response = self._save("legacy-replacement.wav", voice_id=voice_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotEqual(old_bytes, dest.read_bytes())
        self.assertEqual(legacy.read_bytes(), dest.read_bytes())
        updated = self._entries()[-1]
        self.assertNotIn("seed", updated)
        self.assertNotIn("language", updated)

    def test_legacy_same_artifact_resave_keeps_provenance_and_bytes(self):
        """Only a demonstrably unchanged artifact may retain old provenance."""
        original = self._preview(seed=SEED, language="English")
        voice_id = self._save(original["audio_url"].rsplit("/", 1)[-1]).json()["voice_id"]
        entry = self._entries()[-1]
        dest = self.root / entry["filename"]
        old_bytes = dest.read_bytes()

        same = self.previews / "legacy-same.wav"
        same.write_bytes(old_bytes)
        response = self._save("legacy-same.wav", voice_id=voice_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(old_bytes, dest.read_bytes())
        updated = self._entries()[-1]
        self.assertEqual(SEED, updated["seed"])
        self.assertEqual("English", updated["language"])

    def test_verified_save_hashes_the_staged_artifact_that_is_published(self):
        result = self._preview(seed=SEED, language="English")
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        hashed = []
        real_hash = voice_design._sha256_file
        def recording(path):
            hashed.append(Path(path).name)
            return real_hash(path)
        with patch.object(voice_design, "_sha256_file", side_effect=recording):
            response = self._save(preview_file)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(any(name.startswith(".") and name.endswith(".tmp") for name in hashed),
                        "save must hash the staged bytes it publishes, not only the source path")

    def test_empty_language_uses_the_engines_effective_fallback(self):
        """tts.py uses `language or self._language`, so '' is not an effective language."""
        self.engine = RecordingEngine(self.previews, language="English")
        result = self._preview(seed=SEED, language="")
        self.assertEqual("", self.engine.calls[-1]["language"])
        preview_file = result["audio_url"].rsplit("/", 1)[-1]
        response = self._save(preview_file)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("English", self._entries()[-1]["language"])

    def _aliases(self, canonical):
        aliases = [canonical]
        if os.name == "nt":
            # Windows strips trailing dots/spaces when resolving a filename, so
            # these name the same WAV but not the same raw receipt key.
            aliases += [canonical + ".", canonical + " "]
        return aliases

    def test_windows_alias_of_a_preview_uses_its_receipt(self):
        result = self._preview(seed=SEED, language="English")
        canonical = result["audio_url"].rsplit("/", 1)[-1]
        for alias in self._aliases(canonical)[1:]:
            with self.subTest(alias=alias):
                response = self._save(alias, seed=SEED)
                self.assertEqual(200, response.status_code, response.text)
                entry = self._entries()[-1]
                self.assertEqual(SEED, entry["seed"])
                self.assertEqual("English", entry["language"])

    def test_windows_alias_cannot_bypass_the_staged_digest(self):
        result = self._preview(seed=SEED, language="English")
        canonical = result["audio_url"].rsplit("/", 1)[-1]
        (self.previews / canonical).write_bytes(b"mutated preview bytes")
        for alias in self._aliases(canonical)[1:]:
            with self.subTest(alias=alias):
                response = self._save(alias)
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual([], self._entries())

    def test_corrupt_receipt_refuses_before_publication(self):
        result = self._preview(seed=SEED, language="English")
        canonical = result["audio_url"].rsplit("/", 1)[-1]
        voice_id = self._save(canonical).json()["voice_id"]
        dest = self.root / self._entries()[-1]["filename"]
        published_bytes, manifest_bytes = dest.read_bytes(), self.manifest.read_bytes()
        receipt_path = Path(voice_design._preview_receipt_path(canonical))
        receipt_path.write_text("{", encoding="utf-8")
        (self.previews / canonical).write_bytes(b"mutated preview bytes")

        response = self._save(canonical, voice_id=voice_id)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(published_bytes, dest.read_bytes())
        self.assertEqual(manifest_bytes, self.manifest.read_bytes())

    def test_unreadable_receipt_is_not_treated_as_a_legacy_preview(self):
        result = self._preview(seed=SEED, language="English")
        canonical = result["audio_url"].rsplit("/", 1)[-1]
        (self.previews / canonical).write_bytes(b"mutated preview bytes")
        with patch.object(voice_design, "_preview_receipt_path", return_value=str(self.root)):
            response = self._save(canonical)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual([], self._entries())


if __name__ == "__main__":
    unittest.main()
