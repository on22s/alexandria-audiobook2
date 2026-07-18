import hashlib
import tempfile
import unittest
from pathlib import Path

from benchmark_fixtures import build_script_generation_manifest
from benchmark_runner import _load_text_fixture


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


if __name__ == "__main__":
    unittest.main()
