"""Training must not consume the validation split.

WHAT THIS PROTECTS. The dataset builder writes 180 train / 20 val with zero
overlap, and also a root metadata.jsonl holding all 200. `train_lora.py` loaded
the root file unconditionally, so every adapter was trained on its own
validation set - 67 of 75 in the library record num_samples=200.

Nothing failed. The split was right there, correct, and ignored. The cost is
that no adapter in the library can be honestly evaluated: every line available
to test it with has already been seen, so a good score proves only that the
model can repeat its own training data.

WHY A TEST AND NOT JUST THE FIX. The failure is invisible from the outside. A
contaminated adapter trains normally, reports a normal loss, and produces
plausible audio. The only symptom is a number that is too good, months later,
in a document that does not know it is wrong.

Paths inside all three metadata files are relative to the DATASET ROOT, which
is why the fix can change only which file is read.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_dataset(root, n_train, n_val):
    """Write a dataset shaped like the real builder's output."""
    os.makedirs(os.path.join(root, "train"), exist_ok=True)
    train = [{"audio_filepath": f"train/sample_{i}.wav", "text": f"train {i}",
              "duration": 1.0} for i in range(n_train)]
    rows = list(train)
    if n_val:
        os.makedirs(os.path.join(root, "val"), exist_ok=True)
        val = [{"audio_filepath": f"val/sample_{i}.wav", "text": f"val {i}",
                "duration": 1.0} for i in range(n_val)]
        rows += val
        with open(os.path.join(root, "val", "metadata.jsonl"), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in val)
        with open(os.path.join(root, "train", "metadata.jsonl"), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in train)
    with open(os.path.join(root, "metadata.jsonl"), "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    return train


class TrainSplitTest(unittest.TestCase):

    def test_train_lora_reads_the_split_not_the_root(self):
        """Exercise the metadata reader actually used by the trainer."""
        from train_lora import get_training_metadata
        for split in (True, False):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                if split:
                    expected = build_dataset(tmp, 180, 20)
                else:
                    # Preserve the original root-only, bare-relative legacy input.
                    expected = [{"audio_filepath": f"sample_{i}.wav", "text": f"t{i}",
                                 "duration": 1.0} for i in range(40)]
                    with open(os.path.join(tmp, "metadata.jsonl"), "w") as fh:
                        fh.writelines(json.dumps(row) + "\n" for row in expected)
                entries, used_split = get_training_metadata(tmp)
                self.assertEqual(split, used_split)
                self.assertEqual(expected, entries)
                self.assertEqual(180 if split else 40, len(entries))
                for row in entries:
                    self.assertFalse(os.path.isabs(row["audio_filepath"]))
                    if split:
                        self.assertTrue(row["audio_filepath"].startswith("train/"),
                                        "a val/ clip reached the training set")

    def test_missing_train_metadata_never_falls_back_to_partitioned_root(self):
        import wave
        from dataset_metadata import get_training_metadata, get_training_dataset_preflight
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(tmp, 2, 1)
            os.remove(os.path.join(tmp, 'train', 'metadata.jsonl'))
            for remove_val_metadata in (False, True):
                if remove_val_metadata:
                    os.remove(os.path.join(tmp, 'val', 'metadata.jsonl'))
                with self.assertRaisesRegex(ValueError, 'no training metadata'):
                    get_training_metadata(tmp)
        for audio_key, audio_path in (('audio_filepath', 'sample.wav'),
                                      ('audio_filepath', 'train/clip.wav'), ('audio', 'train/clip.wav')):
            with self.subTest(legacy_audio_path=audio_path, key=audio_key), tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, 'metadata.jsonl'), 'w') as stream:
                    stream.write(json.dumps({audio_key: audio_path, 'text': 'Legacy'}) + '\n')
                wav_path = os.path.join(tmp, audio_path)
                os.makedirs(os.path.dirname(wav_path), exist_ok=True)
                with wave.open(wav_path, 'wb') as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                    audio.writeframes(bytes((0, 16)) * 2400)
                rows, split = get_training_metadata(tmp)
                self.assertFalse(split)
                report = get_training_dataset_preflight(tmp)
                self.assertFalse(report['used_split'])
                self.assertEqual(1, report['sample_count'])
                self.assertEqual(audio_path, rows[0][audio_key])
                self.assertEqual('Legacy', rows[0]['text'])

    def test_native_validation_only_zip_is_refused_by_training_and_batch_preflight(self):
        import zipfile
        import wave
        from unittest.mock import patch
        from pathlib import Path
        from dataset_metadata import get_training_metadata, get_training_dataset_preflight
        from tests.test_preparer_run_state import preparer
        from tests.test_voicelab_pipeline_scripts import batch_train
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'dataset_temp'; work.mkdir()
            with wave.open(str(work / 'sample_0000.wav'), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                audio.writeframes(bytes((0, 16)) * 2400)
            rows = [{'audio_filepath': 'sample_0000.wav', 'text': 'Synthetic sample.',
                     'speaker': 'ALICE', 'speaker_labels': ['ALICE'], 'duration': 0.1}]
            output = root / 'dataset.zip'
            with patch.object(preparer, 'get_sample_path',
                              side_effect=lambda directory, name: work / name):
                preparer._create_zip_dataset(rows, str(output), val_split=1.0)
            extracted = root / 'extracted'
            with zipfile.ZipFile(output) as archive:
                self.assertIn('val/metadata.jsonl', archive.namelist())
                self.assertNotIn('train/metadata.jsonl', archive.namelist())
                archive.extractall(extracted)
            for reader in (get_training_metadata, get_training_dataset_preflight):
                with self.assertRaisesRegex(ValueError, 'no training metadata'):
                    reader(str(extracted))
            report = batch_train.get_batch_archive_preflight([str(output)])
            self.assertEqual([], report['datasets'])
            self.assertEqual(1, len(report['errors']))
            self.assertIn('no training metadata', report['errors'][0]['error'])


class LibraryContaminationTest(unittest.TestCase):
    """The evidence that motivated the fix.

    The first version of this skipped when no adapter library was present -
    which is every CI checkout. The release verifier counts a skip as a
    failure, and rightly: a test that quietly does nothing still reports green.
    Three other tests were rewritten for exactly this reason hours before this
    one was written with the same flaw.

    So the logic is exercised on a fixture, and the real library is consulted
    only as a bonus when one happens to be there.
    """

    def _count_contaminated(self, metas):
        """How many adapters record training on the full 200 (train+val)."""
        counts = []
        for path in metas:
            try:
                with open(path, encoding="utf-8") as handle:
                    counts.append(json.load(handle).get("num_samples"))
            except (OSError, ValueError):
                continue
        return sum(1 for c in counts if c == 200), len(counts)

    def test_the_contamination_signature_is_detectable(self):
        """num_samples == 200 on a 180/20 dataset means both splits were
        consumed. This is the check that identified 67 of 75 adapters."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, n in (("clean", 180), ("contaminated", 200),
                            ("also_clean", 180)):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                with open(os.path.join(d, "training_meta.json"), "w") as fh:
                    json.dump({"num_samples": n}, fh)
            metas = [os.path.join(tmp, n, "training_meta.json")
                     for n in ("clean", "contaminated", "also_clean")]
            self.assertEqual(self._count_contaminated(metas), (1, 3))

    def test_unreadable_metadata_is_not_counted_either_way(self):
        """A corrupt file must not silently inflate or deflate the count."""
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "training_meta.json")
            with open(good, "w") as fh:
                json.dump({"num_samples": 200}, fh)
            bad = os.path.join(tmp, "broken.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            self.assertEqual(self._count_contaminated([good, bad]), (1, 1))

if __name__ == "__main__":
    unittest.main()
