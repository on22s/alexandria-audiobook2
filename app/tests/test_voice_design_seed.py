"""The Designer preview's `seed` is a real input, not a discarded field.

Fail-first coverage for #2140. The HTTP boundary used to accept `seed` through
Pydantic's extra="ignore", forward the engine default (-1), and answer 200, so
a caller's pinned request silently produced a random voice. These tests drive
the REAL request schema, the REAL FastAPI route and the REAL manifest writer;
only the engine is stubbed. No GPU, no billable inference, no user data.
"""
import copy
import json
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
    def __init__(self, directory):
        self.directory = Path(directory)
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
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values():
            row["running"] = False
        overrides = (
            ("process_state", self.state),
            ("_task_claims", {}),
            ("_gpu_leases", {}),
            ("DATA_DIR", str(self.root)),
            ("acquire_gpu_lock", lambda *a, **k: None),
            ("check_global_gpu_lock", lambda *a, **k: None),
            ("llm_is_on_this_gpu", lambda *a, **k: True),
        )
        for name, value in overrides:
            ctx = patch.object(core, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def _app(self, engine):
        app = FastAPI()
        app.include_router(voice_design.router)
        engine_ctx = patch.object(voice_design.project_manager, "get_engine", return_value=engine)
        engine_ctx.start()
        self.addCleanup(engine_ctx.stop)
        return app

    def _preview(self, engine, seed=None):
        payload = {"description": "A synthetic narrator", "sample_text": "Synthetic sample line."}
        if seed is not None:
            payload["seed"] = seed
        app = self._app(engine)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/voice_design/preview", json=payload)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    # --- schema boundary -----------------------------------------------------

    def test_request_schema_accepts_a_seed_and_defaults_to_random(self):
        pinned = voice_design.VoiceDesignPreviewRequest(
            description="A synthetic narrator", sample_text="Synthetic sample line.", seed=SEED)
        self.assertEqual(SEED, pinned.model_dump()["seed"])
        omitted = voice_design.VoiceDesignPreviewRequest(
            description="A synthetic narrator", sample_text="Synthetic sample line.")
        self.assertEqual(-1, omitted.model_dump()["seed"])

    def test_request_schema_rejects_an_unsupported_seed_instead_of_ignoring_it(self):
        """A negative seed other than the -1 sentinel cannot be honoured: fail loud."""
        with self.assertRaises(ValidationError):
            voice_design.VoiceDesignPreviewRequest(
                description="A synthetic narrator", sample_text="Synthetic sample line.", seed=-2)

    # --- HTTP dispatch -> engine boundary ------------------------------------

    def test_route_forwards_seed_and_omission_stays_random(self):
        pinned_engine = RecordingEngine(self.root / "pinned")
        self._preview(pinned_engine, seed=SEED)
        self.assertEqual([SEED], [call["seed"] for call in pinned_engine.calls])

        random_engine = RecordingEngine(self.root / "random")
        self._preview(random_engine)
        self.assertEqual([-1], [call["seed"] for call in random_engine.calls])

    def test_same_seed_reproduces_audio_and_a_different_seed_does_not(self):
        """Synthetic seeded engine: reproducibility is checked without real synthesis."""
        engine = RecordingEngine(self.root / "repro")
        first = self._preview(engine, seed=SEED)
        second = self._preview(engine, seed=SEED)
        third = self._preview(engine, seed=OTHER_SEED)

        outputs = {path.name: path.read_bytes() for path in engine.outputs}
        first_bytes = outputs[first["audio_url"].rsplit("/", 1)[-1]]
        second_bytes = outputs[second["audio_url"].rsplit("/", 1)[-1]]
        third_bytes = outputs[third["audio_url"].rsplit("/", 1)[-1]]
        self.assertEqual(first_bytes, second_bytes)
        self.assertNotEqual(first_bytes, third_bytes)

    def test_seed_is_per_request_and_leaves_task_state_clean(self):
        engine = RecordingEngine(self.root / "scoped")
        self._preview(engine, seed=11)
        self._preview(engine, seed=22)
        self.assertEqual([11, 22], [call["seed"] for call in engine.calls])
        self.assertFalse(self.state["voice_design"]["running"])
        self.assertNotIn("voice_design", core._task_claims)

    # --- manifest provenance -------------------------------------------------

    def test_save_persists_the_seed_and_records_random_when_omitted(self):
        previews = self.root / "previews"
        _write_tone(previews / "seeded.wav", SEED)
        _write_tone(previews / "random.wav", -1)
        manifest = self.root / "manifest.json"
        with patch.object(voice_design, "DESIGNED_VOICES_DIR", str(self.root)), \
                patch.object(voice_design, "DESIGNED_VOICES_MANIFEST", str(manifest)):
            app = FastAPI()
            app.include_router(voice_design.router)
            base = {"name": "Synthetic voice", "description": "A synthetic narrator",
                    "sample_text": "Synthetic sample line."}
            with TestClient(app, raise_server_exceptions=False) as client:
                pinned = client.post("/api/voice_design/save",
                                     json={**base, "preview_file": "seeded.wav", "seed": SEED})
                random = client.post("/api/voice_design/save",
                                     json={**base, "preview_file": "random.wav"})
        self.assertEqual(200, pinned.status_code, pinned.text)
        self.assertEqual(200, random.status_code, random.text)
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(SEED, entries[0]["seed"])
        self.assertEqual(-1, entries[1]["seed"])


if __name__ == "__main__":
    unittest.main()
