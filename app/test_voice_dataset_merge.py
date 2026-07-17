import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np
import soundfile as sf

from voice_dataset_merge import get_source_records, is_reusable_merge, merge_voice_datasets


class VoiceDatasetMergeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
