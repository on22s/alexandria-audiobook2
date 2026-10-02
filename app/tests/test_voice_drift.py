"""Voice-drift check: a rendered chunk that doesn't sound like its speaker's
reference is flagged; an unmeasured run is reported, never passed."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import core as core_module
import voice_drift
from project import ProjectManager



def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"RIFF")
    return path


class VoiceDriftScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.chunks = [
            {"id": 0, "uid": "u0", "speaker": "HOLO", "status": "done", "audio_path": "voicelines/a.mp3"},
            {"id": 1, "uid": "u1", "speaker": "HOLO", "status": "done", "audio_path": "voicelines/b.mp3"},
            {"id": 2, "uid": "u2", "speaker": "LAWRENCE", "status": "done", "audio_path": "voicelines/c.mp3"},
            {"id": 3, "uid": "u3", "speaker": "NARRATOR", "status": "done", "audio_path": "voicelines/d.mp3"},
            {"id": 4, "uid": "u4", "speaker": "NARRATOR", "status": "done", "audio_path": "voicelines/e.mp3"},
            {"id": 5, "uid": "u5", "speaker": "HOLO", "status": "pending", "audio_path": None},
        ]
        for c in self.chunks:
            if c["audio_path"]:
                _touch(os.path.join(self.root, c["audio_path"]))
        _touch(os.path.join(self.root, "clone_voices", "holo.wav"))
        _touch(os.path.join(self.root, "lora_models", "lawrence", "ref_sample.wav"))
        self.voice_config = {
            "HOLO": {"type": "clone", "ref_audio": "clone_voices/holo.wav"},
            "LAWRENCE": {"type": "lora", "adapter_path": "lora_models/lawrence"},
            "NARRATOR": {"type": "design", "description": "warm baritone"},
        }
        self.resolve = lambda rel: os.path.join(self.root, rel)
        # No pydub in the test: pretend decoding is the identity.
        self._decode = patch.object(voice_drift, "_decode_to_wav", side_effect=lambda src, d, stem: src)
        self._decode.start()

    def tearDown(self):
        self._decode.stop()
        self.tmp.cleanup()

    def _run(self, scores, threshold=0.45, indices=None, err=None):
        seen = {}
        def fake_scorer(pairs, python_bin):
            seen["pairs"] = pairs
            return ([None] * len(pairs), err) if err else (scores[:len(pairs)], None)
        report = voice_drift.check_voice_drift(
            self.chunks, self.voice_config, self.root, "/usr/bin/python3", threshold,
            indices=indices, resolve_asset_path=self.resolve, score_pairs=fake_scorer)
        return report, seen

    def test_references_follow_voice_type_and_flags_follow_threshold(self):
        report, seen = self._run([0.72, 0.31, 0.66, 0.44])
        self.assertIsNone(report["error"])
        by_uid = {r["uid"]: r for r in report["results"]}
        # clone -> its ref_audio, lora -> adapter ref_sample, design -> earliest done chunk
        pairs = seen["pairs"]
        self.assertTrue(pairs[0][1].endswith("clone_voices/holo.wav"))
        self.assertTrue(pairs[2][1].endswith("lora_models/lawrence/ref_sample.wav"))
        self.assertTrue(pairs[3][1].endswith("voicelines/d.mp3"))
        self.assertEqual(by_uid["u0"]["reference"], "clone:HOLO")
        self.assertEqual(by_uid["u2"]["reference"], "lora:LAWRENCE")
        self.assertEqual(by_uid["u3"], {"index": 3, "uid": "u3", "score": None,
                                        "flagged": False, "reference": "self",
                                        "error": "not measured: chunk is its own reference"})
        self.assertEqual(by_uid["u4"]["reference"], "chunk:u3")
        # exactly the below-threshold ones are flagged; 0.44 < 0.45 counts
        self.assertEqual({u for u, r in by_uid.items() if r["flagged"]}, {"u1", "u4"})
        self.assertEqual(by_uid["u1"]["score"], 0.31)
        self.assertNotIn("u5", by_uid)  # pending chunks are not scored

    def test_duplicate_reference_uid_keeps_first_match_and_path_refusal(self):
        chunks = [
            {"uid": ["malformed"], "status": "pending"},
            {"uid": "reference", "speaker": "A", "status": "done",
             "audio_path": "../outside.wav"},
            {"uid": "reference", "speaker": "A", "status": "done",
             "audio_path": "voicelines/a.mp3"},
        ] + [{"uid": f"target-{i}", "speaker": "A", "status": "done",
              "audio_path": "voicelines/b.mp3"} for i in range(65)]
        with patch.object(voice_drift, "_decode_to_wav") as decode:
            report = voice_drift.check_voice_drift(
                chunks, {}, self.root, "/usr/bin/python3", .45,
                indices=list(range(3, len(chunks))), score_pairs=lambda *args: ([], None))
        self.assertIsNone(report["error"])
        self.assertEqual(len(report["results"]), 65)
        self.assertTrue(all(row["error"] == "reference audio path is outside project"
                            for row in report["results"]))
        self.assertTrue(all(row["score"] is None and not row["flagged"]
                            for row in report["results"]))
        decode.assert_not_called()

    def test_indices_restrict_which_chunks_are_scored(self):
        report, seen = self._run([0.9], indices=[1])
        self.assertEqual([r["uid"] for r in report["results"]], ["u1"])
        self.assertEqual(len(seen["pairs"]), 1)

    def test_unmeasured_runs_flag_nothing_and_say_so(self):
        report, _ = self._run([], err="rc=2 speechbrain unavailable")
        self.assertEqual(report["results"], [])
        self.assertTrue(report["error"].startswith("not measured: rc=2"))
        report = voice_drift.check_voice_drift(self.chunks, self.voice_config, self.root, None, 0.45)
        self.assertEqual(report["results"], [])
        self.assertIn("no speechbrain interpreter", report["error"])

    def test_missing_reference_is_reported_per_chunk_not_flagged(self):
        os.remove(os.path.join(self.root, "clone_voices", "holo.wav"))
        report, seen = self._run([0.2, 0.9, 0.9])
        by_uid = {r["uid"]: r for r in report["results"]}
        self.assertEqual(by_uid["u0"]["error"], "reference audio missing")
        self.assertFalse(by_uid["u0"]["flagged"])
        self.assertIsNone(by_uid["u0"]["score"])
        self.assertFalse(any(p[1].endswith("holo.wav") for p in seen["pairs"]))

    def test_threshold_comes_from_config_or_default(self):
        self.assertEqual(voice_drift.get_drift_threshold(None), 0.45)
        self.assertEqual(voice_drift.get_drift_threshold({"voice_drift_min_similarity": 0.6}), 0.6)
        self.assertEqual(voice_drift.get_drift_threshold({"voice_drift_min_similarity": "nope"}), 0.45)
        self.assertEqual(voice_drift.get_drift_threshold({"voice_drift_min_similarity": 3}), 0.45)


class VoiceDriftPersistenceTests(unittest.TestCase):
    def test_results_are_written_onto_chunks_and_cleared_by_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            chunks = [{"id": 0, "uid": "u0", "speaker": "HOLO", "text": "hi", "status": "done",
                       "audio_path": "voicelines/a.mp3"}]
            with open(pm.chunks_path, "w", encoding="utf-8") as f:
                json.dump(chunks, f)
            flagged = voice_drift.apply_drift_results(
                pm, [{"index": 0, "uid": "u0", "score": 0.3, "flagged": True, "reference": "clone:HOLO"}],
                0.45)
            self.assertEqual(flagged, 1)
            with open(pm.chunks_path, encoding="utf-8") as f:
                saved = json.load(f)[0]["drift"]
            self.assertEqual((saved["score"], saved["flagged"], saved["reference"], saved["threshold"]),
                             (0.3, True, "clone:HOLO", 0.45))
            # Regenerating the chunk produces new audio, so the verdict is dropped.
            _touch(os.path.join(tmp, "temp_batch_0.wav"))
            chunks = pm.load_chunks()
            with patch("project.validate_generated_audio"), \
                 patch.object(pm, "_export_chunk_audio") as export:
                def write_staged_audio(temp_path, filename_base):
                    relative = f"voicelines/{filename_base}.wav"
                    _touch(os.path.join(tmp, relative))
                    return relative
                export.side_effect = write_staged_audio
                outcome = pm._finalize_completed_chunk(0, chunks)
            self.assertEqual(outcome[0], "completed")
            self.assertIsNone(pm.load_chunks()[0]["drift"])
            self.assertEqual(saved, chunks[0]["drift"])

    def test_speaker_model_interpreter_prefers_the_running_env(self):
        import importlib.util
        import voice_reference
        cfg = {"rocm_python": "/opt/rocm_py"}
        # speechbrain importable here -> this interpreter, no cross-repo hop
        with patch.object(importlib.util, "find_spec", return_value=object()):
            self.assertEqual(voice_drift.get_speaker_model_python(cfg), sys.executable)
        # not importable -> Voice Lab's configured interpreter when it exists
        with patch.object(importlib.util, "find_spec", return_value=None), \
             patch.object(voice_reference.os.path, "exists", side_effect=lambda p: p == "/opt/rocm_py"):
            self.assertEqual(voice_drift.get_speaker_model_python(cfg), "/opt/rocm_py")
        # then the sibling repo's env; None when nothing has it (-> NOT MEASURED)
        with patch.object(importlib.util, "find_spec", return_value=None), \
             patch.object(voice_reference.os.path, "exists", side_effect=lambda p: p == voice_reference.SIBLING_PY):
            self.assertEqual(voice_drift.get_speaker_model_python(cfg), voice_reference.SIBLING_PY)
        with patch.object(importlib.util, "find_spec", return_value=None), \
             patch.object(voice_reference.os.path, "exists", return_value=False):
            self.assertIsNone(voice_drift.get_speaker_model_python(cfg))

    def test_drift_check_is_exempt_from_the_gpu_lock(self):
        self.assertIn("drift_check", core_module.NON_GPU_TASKS)
        self.assertIn("drift_check", core_module.process_state)
        with patch.dict(core_module.process_state["audio"], {"running": True}):
            core_module.check_global_gpu_lock("drift_check")  # must not raise


if __name__ == "__main__":
    unittest.main()


class DriftCheckRouteTests(unittest.TestCase):
    """The literal /api/chunks/drift_check must win over /api/chunks/{index}.
    Registered below the integer wildcard (as it was from #525 until this
    test), every request was a 422 'unable to parse drift_check as an
    integer' and the Editor button never ran a check."""

    def test_drift_check_route_is_not_swallowed_by_chunk_index(self):
        from fastapi.testclient import TestClient
        import app as app_module
        state = core_module.process_state["drift_check"]
        previous = state["running"]
        state["running"] = True   # a claimed task: the endpoint itself must answer, with 400
        try:
            response = TestClient(app_module.app).post("/api/chunks/drift_check", json={"indices": []})
        finally:
            state["running"] = previous
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("already running", response.json()["detail"])


class DriftFailureArtifactTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import soundfile as sf
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chunks = [{"uid": f"u{i}", "speaker": "ANN", "text": str(i),
                        "status": "done", "audio_path": f"clip{i}.wav"} for i in range(3)]
        for name in ["reference.wav"] + [c["audio_path"] for c in self.chunks]:
            sf.write(self.root / name, np.sin(np.arange(4000) / 20) * 0.2, 16000)
        self.config = {"ANN": {"type": "clone", "ref_audio": "reference.wav"}}
        self.manager = ProjectManager(str(self.root))
        with open(self.manager.chunks_path, "w", encoding="utf-8") as handle:
            json.dump(self.chunks, handle)

    def run_check(self, scores, **options):
        return voice_drift.check_voice_drift(
            self.chunks, self.config, str(self.root), sys.executable, 0.45,
            resolve_asset_path=lambda p: str(self.root / p),
            score_pairs=lambda pairs, python: (scores, None), **options)

    def test_invalid_score_values_are_unmeasured_in_saved_artifacts(self):
        import copy
        for invalid in (float("nan"), float("inf"), -float("inf"),
                        "NaN", "bad", {}, [], True):
            with self.subTest(score=repr(invalid)):
                original = copy.deepcopy((self.chunks, self.config))
                report = self.run_check([0.7, invalid, 0.2])
                self.assertIsNone(report["error"])
                self.assertEqual(["u0", "u1", "u2"], [r["uid"] for r in report["results"]])
                bad = report["results"][1]
                self.assertIsNone(bad["score"])
                self.assertFalse(bad["flagged"])
                self.assertIn("invalid", bad["error"])
                self.assertEqual(1, voice_drift.apply_drift_results(self.manager, report["results"], 0.45))
                saved = self.manager.load_chunks()
                json.dumps(saved, allow_nan=False)
                self.assertIsNone(saved[1]["drift"]["score"])
                self.assertIn("invalid", saved[1]["drift"]["error"])
                self.assertEqual(0.7, saved[0]["drift"]["score"])
                self.assertTrue(saved[2]["drift"]["flagged"])
                self.assertEqual(original, (self.chunks, self.config))

    def test_incomplete_or_malformed_score_lists_cannot_publish_partial_measurement(self):
        for scores in ([], [0.7], [0.7] * 4, None, "0.7", {"0": 0.7}):
            with self.subTest(scores=scores):
                before = Path(self.manager.chunks_path).read_bytes()
                report = self.run_check(scores)
                self.assertEqual([], report["results"])
                self.assertIn("not measured", report["error"])
                self.assertIn("score", report["error"])
                self.assertEqual(before, Path(self.manager.chunks_path).read_bytes())

    def test_self_reference_is_explicitly_unmeasured_on_disk(self):
        self.config = {"ANN": {"type": "custom", "voice": "Ryan"}}
        report = self.run_check([], indices=[0])
        result = report["results"][0]
        self.assertIsNone(result["score"])
        self.assertEqual("self", result["reference"])
        self.assertIn("not measured", result["error"])
        self.assertEqual(0, voice_drift.apply_drift_results(self.manager, report["results"], 0.45))
        saved = self.manager.load_chunks()[0]["drift"]
        self.assertIsNone(saved["score"])
        self.assertIn("not measured", saved["error"])

    def test_unresolved_lora_asset_is_missing_reference_not_a_request_crash(self):
        reference = voice_drift.get_reference_for_speaker(
            "ANN", {"ANN": {"type": "lora", "adapter_path": "missing"}},
            self.chunks, None, lambda p: None)
        self.assertEqual((None, "lora:ANN"), reference)

    def test_failed_decode_does_not_shift_successful_scores_to_wrong_chunk(self):
        # A real corrupt first file must not consume the owner of the next
        # valid pair. Both later chunks still decode through pydub/FFmpeg.
        (self.root / "clip0.wav").write_bytes(b"not audio")
        report = self.run_check([0.7, 0.2])
        self.assertIsNone(report["error"])
        by_uid = {r["uid"]: r for r in report["results"]}
        self.assertEqual({"u0", "u1", "u2"}, set(by_uid))
        self.assertIsNone(by_uid["u0"]["score"])
        self.assertIn("decode failed", by_uid["u0"]["error"])
        self.assertEqual(0.7, by_uid["u1"]["score"])
        self.assertEqual(0.2, by_uid["u2"]["score"])
        self.assertEqual(1, voice_drift.apply_drift_results(self.manager, report["results"], 0.45))
        saved = self.manager.load_chunks()
        self.assertIsNone(saved[0]["drift"]["score"])
        self.assertEqual(0.7, saved[1]["drift"]["score"])
        self.assertTrue(saved[2]["drift"]["flagged"])
