"""A new builder batch clears a stale cancel through the claim, not in the worker."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
from routers import dataset_builder as routes


class RecordingEngine:
    def __init__(self, directory, block=False):
        self.directory = Path(directory)
        self.rendered = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def generate_voice_design(self, *, description, sample_text, seed):
        self.rendered.append(sample_text)
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test engine was not released")
        path = self.directory / "preview.wav"
        path.write_bytes(b"preview")
        return str(path), 16000


class DatasetBuilderCancelClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.builder = root / "builder"
        self.builder.mkdir()
        self.previews = root / "previews"
        self.previews.mkdir()
        data = root / "data"
        data.mkdir()
        work = self.builder / "voice"
        work.mkdir()
        (work / "state.json").write_text(json.dumps({
            "description": "warm", "global_seed": "", "samples": [
                {"emotion": "", "text": "One", "seed": "", "description": "warm",
                 "status": "pending", "audio_url": None},
                {"emotion": "", "text": "Two", "seed": "", "description": "warm",
                 "status": "pending", "audio_url": None}]}))
        states = copy.deepcopy(core.process_state)
        for state in states.values():
            state["running"] = False
        states["dataset_builder"]["cancel"] = False
        self.states = states
        self.patches = [
            patch.object(routes, "DATASET_BUILDER_DIR", str(self.builder)),
            patch.object(core, "DATA_DIR", str(data)),
            patch.object(core, "process_state", states),
            patch.object(routes, "process_state", states),
            patch.object(core, "_task_claims", {}),
            patch.object(core, "_gpu_leases", {}),
            patch.object(core, "llm_is_on_this_gpu", return_value=True),
            patch.object(core, "acquire_gpu_lock", return_value=None),
            patch.object(core.project_manager, "ensure_idle_local_models_released",
                         return_value=True),
            patch.object(routes, "check_global_gpu_lock"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name, owner in list(core._task_claims.items()):
            core.release_gpu_task_claim(name, owner["id"])

    def request(self):
        return routes.DatasetBatchGenRequest(
            name="voice", description="warm",
            samples=[{"text": "One"}, {"text": "Two"}], indices=[0, 1],
            expected_count=2)

    def wait_finished(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (not self.states["dataset_builder"]["running"]
                    and "dataset_builder" not in core._task_claims):
                return
            time.sleep(0.01)
        self.fail("dataset builder worker did not finish")

    def test_stale_cancel_does_not_stop_a_new_batch(self):
        self.states["dataset_builder"]["cancel"] = True
        engine = RecordingEngine(self.previews)
        with patch.object(routes.project_manager, "get_engine", return_value=engine):
            started = routes._dataset_builder_generate_batch(self.request())
        self.assertEqual("started", started["status"])
        self.assertFalse(self.states["dataset_builder"]["cancel"],
                         "the claim, not the worker, must clear the stale cancel")
        self.wait_finished()
        self.assertEqual(["One", "Two"], engine.rendered)
        self.assertFalse(self.states["dataset_builder"]["cancel"])
        self.assertIn("[DONE] Generated 2/2 samples",
                      self.states["dataset_builder"]["logs"])

    def test_cancel_right_after_start_stops_the_batch(self):
        engine = RecordingEngine(self.previews, block=True)
        self.addCleanup(engine.release.set)
        with patch.object(routes.project_manager, "get_engine", return_value=engine):
            routes._dataset_builder_generate_batch(self.request())
            self.assertTrue(engine.entered.wait(5))
            cancelled = asyncio.run(routes.dataset_builder_cancel("voice"))
            self.assertEqual({"status": "cancelling"}, cancelled)
            engine.release.set()
        self.wait_finished()
        self.assertEqual(["One"], engine.rendered)
        self.assertIn("[CANCEL] Stopped at 1/2",
                      self.states["dataset_builder"]["logs"])
