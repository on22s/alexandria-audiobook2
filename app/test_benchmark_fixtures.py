import hashlib
import tempfile
import unittest
from pathlib import Path

from benchmark_fixtures import (build_script_generation_manifest,
                                build_script_review_manifest,
                                build_tts_clone_manifest,
                                build_tts_design_manifest,
                                build_tts_generation_manifest)
from benchmark_runner import _load_review_fixture, _load_text_fixture


class BenchmarkFixtureTests(unittest.TestCase):
    def test_manifest_reconstructs_hashed_production_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text(("First paragraph. " * 30) + "\n\n" +
                            ("Second paragraph. " * 30), encoding="utf-8")
            context = [{"speaker": "NARRATOR", "text": "Earlier."}]
            manifest = build_script_generation_manifest([{
                "path": str(path), "chunk_numbers": [2],
                "previous_entries_by_chunk": {2: context}}], tmp, chunk_size=300)
            fixture = manifest["fixtures"][0]
            reconstructed = _load_text_fixture(fixture, tmp)
        self.assertEqual(2, fixture["chunk_number"])
        self.assertEqual(context, fixture["previous_entries"])
        self.assertEqual(fixture["sha256"],
                         hashlib.sha256(reconstructed.encode("utf-8")).hexdigest())

    def test_manifest_rejects_out_of_range_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Only one chunk.", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "out of range"):
                build_script_generation_manifest(
                    [{"path": str(path), "chunk_numbers": [2]}], tmp)

    def test_source_drift_is_detected_before_chunk_reconstruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.txt")
            path.write_text("Original source.", encoding="utf-8")
            manifest = build_script_generation_manifest(
                [{"path": str(path), "chunk_numbers": [1]}], tmp)
            path.write_text("Changed source.", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source hash changed"):
                _load_text_fixture(manifest["fixtures"][0], tmp)

    def test_review_manifest_reconstructs_hashed_entry_slice(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.json")
            entries = [{"speaker": "NARRATOR", "text": f"line {index}"}
                       for index in range(30)]
            path.write_text(json.dumps(entries), encoding="utf-8")
            manifest = build_script_review_manifest(
                [{"path": str(path), "entry_starts": [3]}], tmp, batch_size=4)
            fixture = manifest["fixtures"][0]
            reconstructed = _load_review_fixture(fixture, tmp)
        self.assertEqual(entries[2:6], reconstructed)
        self.assertEqual(entries[:2], fixture["previous_tail"])

    def test_review_source_drift_is_rejected(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "book.json")
            path.write_text(json.dumps([{"speaker": "N", "text": "one"}]),
                            encoding="utf-8")
            manifest = build_script_review_manifest(
                [{"path": str(path), "entry_starts": [1]}], tmp)
            path.write_text(json.dumps([{"speaker": "N", "text": "changed"}]),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source hash changed"):
                _load_review_fixture(manifest["fixtures"][0], tmp)

    def test_tts_manifest_is_self_contained_and_hashes_generation_inputs(self):
        manifest = build_tts_generation_manifest([{
            "id": "short", "text": "The door opened.",
            "instruct": "Quiet, tense narration.", "voice": "Ryan", "seed": 7}])
        fixture = manifest["fixtures"][0]
        self.assertEqual("tts_generation", manifest["stage"])
        self.assertEqual("The door opened.", fixture["text"])
        self.assertEqual(64, len(fixture["sha256"]))

    def test_tts_manifest_rejects_random_seed(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            build_tts_generation_manifest([{"text": "Hello.", "seed": -1}])

    def test_clone_manifest_hashes_reference_audio_and_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "ref.wav")
            ref.write_bytes(b"reference audio")
            manifest = build_tts_clone_manifest([{
                "text": "New words.", "ref_audio": "ref.wav",
                "ref_text": "Words spoken in the reference.", "seed": 4}], tmp)
        fixture = manifest["fixtures"][0]
        self.assertEqual("clone", fixture["voice_type"])
        self.assertEqual(hashlib.sha256(b"reference audio").hexdigest(),
                         fixture["ref_audio_sha256"])

    def test_design_manifest_hashes_description_text_and_seed(self):
        manifest = build_tts_design_manifest([{
            "text": "Welcome home.",
            "description": "A warm, low baritone with gentle authority.",
            "seed": 9}])
        fixture = manifest["fixtures"][0]
        self.assertEqual("design", fixture["voice_type"])
        self.assertEqual(64, len(fixture["sha256"]))


if __name__ == "__main__":
    unittest.main()
