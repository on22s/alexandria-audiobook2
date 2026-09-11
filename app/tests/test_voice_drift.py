"""Voice-drift check: a rendered chunk that doesn't sound like its speaker's
reference is flagged; an unmeasured run is reported, never passed."""
import glob
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

REPO = Path(__file__).parent.parent.parent


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
                                        "flagged": False, "reference": "self"})
        self.assertEqual(by_uid["u4"]["reference"], "chunk:u3")
        # exactly the below-threshold ones are flagged; 0.44 < 0.45 counts
        self.assertEqual({u for u, r in by_uid.items() if r["flagged"]}, {"u1", "u4"})
        self.assertEqual(by_uid["u1"]["score"], 0.31)
        self.assertNotIn("u5", by_uid)  # pending chunks are not scored

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
                 patch.object(pm, "_export_chunk_audio", return_value="voicelines/a2.mp3"):
                outcome = pm._finalize_completed_chunk(0, chunks)
            self.assertEqual(outcome[0], "completed")
            self.assertIsNone(chunks[0]["drift"])

    def test_drift_check_is_exempt_from_the_gpu_lock(self):
        self.assertIn("drift_check", core_module.NON_GPU_TASKS)
        self.assertIn("drift_check", core_module.process_state)
        with patch.dict(core_module.process_state["audio"], {"running": True}):
            core_module.check_global_gpu_lock("drift_check")  # must not raise


class VoiceDriftInstrumentTests(unittest.TestCase):
    """Hand-checkable: two halves of one voice's reference clip must score
    higher than that voice against another voice, and above the threshold.
    Runs only where the speechbrain interpreter and two reference clips exist."""

    def test_same_voice_beats_different_voice(self):
        python_bin = voice_drift.get_speaker_model_python(core_module._load_voicelab_config())
        clips = sorted(glob.glob(str(REPO / "lora_models" / "*" / "ref_sample.wav")))[:2]
        if not python_bin or len(clips) < 2:
            self.skipTest("needs the sibling speechbrain interpreter and two lora_models ref clips")
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            a, sr = sf.read(clips[0])
            half = len(a) // 2
            a1, a2 = os.path.join(tmp, "a1.wav"), os.path.join(tmp, "a2.wav")
            sf.write(a1, a[:half], sr)
            sf.write(a2, a[half:], sr)
            scores, err = voice_drift.ecapa_pairs([[a1, a2], [a1, clips[1]]], python_bin)
            self.assertIsNone(err, err)
            same, different = scores
            self.assertGreater(same, different)
            self.assertGreater(same, voice_drift.DRIFT_MIN_SIMILARITY)


if __name__ == "__main__":
    unittest.main()
