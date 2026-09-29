import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np
import soundfile as sf

from voice_dataset_merge import get_pcm_hash, get_source_records, is_reusable_merge, merge_voice_datasets


class VoiceDatasetMergeTests(unittest.TestCase):
    def make_same_pcm_at_rate(self, rate):
        output = io.BytesIO()
        sf.write(output, np.arange(16, dtype=np.float32) / 128, rate,
                 format="WAV", subtype="FLOAT")
        return output.getvalue()

    def test_identical_samples_at_different_rates_remain_distinct_clips(self):
        low = self.make_same_pcm_at_rate(16000)
        high = self.make_same_pcm_at_rate(24000)
        low_pcm, low_rate = sf.read(io.BytesIO(low))
        high_pcm, high_rate = sf.read(io.BytesIO(high))
        np.testing.assert_array_equal(low_pcm, high_pcm)
        self.assertNotEqual(low_rate, high_rate)
        self.assertNotEqual(get_pcm_hash(low), get_pcm_hash(high))
        self.assertEqual(get_pcm_hash(low), get_pcm_hash(self.make_same_pcm_at_rate(16000)))
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp, "source.zip"), Path(tmp, "merged.zip")
            self.make_zip(source, [low, high, low])
            merge_voice_datasets([source], output)
            with zipfile.ZipFile(output) as archive:
                rows = [json.loads(line) for line in archive.read("metadata.jsonl").splitlines()]
                rates = [sf.read(io.BytesIO(archive.read(row["audio_filepath"])))[1]
                         for row in rows]
                manifest = json.loads(archive.read("merge_manifest.json"))
            self.assertEqual([16000, 24000], rates)
            self.assertEqual(1, manifest["duplicate_clip_count"])

    def test_old_merge_cache_is_rebuilt_with_distinct_sample_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp, "source.zip"), Path(tmp, "merged.zip")
            self.make_zip(source, [self.make_same_pcm_at_rate(16000),
                                   self.make_same_pcm_at_rate(24000)])
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("merge_manifest.json", json.dumps({
                    "version": 1, "sources": get_source_records([source])}))
                archive.writestr("metadata.jsonl", "old incomplete merge")
            result = merge_voice_datasets([source], output)
            self.assertEqual("merged", result["status"])
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(2, len(archive.read("metadata.jsonl").splitlines()))
            self.assertEqual("reused", merge_voice_datasets([source], output)["status"])

    def make_wav(self, frequency):
        output = io.BytesIO()
        times = np.arange(8000, dtype=np.float32) / 16000
        sf.write(output, 0.1 * np.sin(2 * np.pi * frequency * times), 16000,
                 format="WAV", subtype="FLOAT")
        return output.getvalue()

    def make_zip(self, path, clips):
        with zipfile.ZipFile(path, "w") as archive:
            metadata = []
            for index, wav in enumerate(clips):
                name = f"train/sample_{index}.wav"
                archive.writestr(name, wav)
                metadata.append({"audio_filepath": name, "text": f"line {index}"})
            archive.writestr("metadata.jsonl", "".join(json.dumps(item) + "\n" for item in metadata))
            archive.writestr("ref.wav", clips[0])

    def test_merge_preserves_unique_clips_provenance_and_removes_pcm_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second, output = Path(tmp, "one.zip"), Path(tmp, "two.zip"), Path(tmp, "merged.zip")
            shared = self.make_wav(180)
            self.make_zip(first, [shared, self.make_wav(220)])
            self.make_zip(second, [shared, self.make_wav(260)])

            result = merge_voice_datasets([second, first], output)
            with zipfile.ZipFile(output) as archive:
                metadata = [json.loads(line) for line in archive.read("metadata.jsonl").splitlines()]
                manifest = json.loads(archive.read("merge_manifest.json"))

            self.assertEqual("merged", result["status"])
            self.assertEqual(3, len(metadata))
            self.assertEqual(1, manifest["duplicate_clip_count"])
            self.assertEqual(4, len(manifest["provenance"]))
            self.assertTrue(is_reusable_merge(output, get_source_records([first, second])))
            self.assertEqual("reused", merge_voice_datasets([first, second], output)["status"])

    def test_failed_merge_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad, output = Path(tmp, "bad.zip"), Path(tmp, "merged.zip")
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("unrelated.txt", "data")
            output.write_bytes(b"previous")
            with self.assertRaisesRegex(ValueError, "metadata.jsonl"):
                merge_voice_datasets([bad], output)
            self.assertEqual(b"previous", output.read_bytes())

    def test_invalid_metadata_is_reported_as_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad, output = Path(tmp, "bad.zip"), Path(tmp, "merged.zip")
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("metadata.jsonl", "{not-json}\n")
            with self.assertRaisesRegex(ValueError, "metadata.jsonl is invalid"):
                merge_voice_datasets([bad], output)


if __name__ == "__main__":
    unittest.main()
