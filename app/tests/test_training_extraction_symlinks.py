"""Exercise real ZIP extraction against pre-existing destination symlinks."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('batch_training_extraction_test', ROOT / 'tools/voice_lab/batch_train_lora.py')
training = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(training)


class TrainingExtractionSymlinkTests(unittest.TestCase):
    def make_zip(self, root, nested=False):
        archive = root / 'dataset.zip'
        prefix = 'dataset/' if nested else ''
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr(prefix + 'metadata.jsonl', '{"text":"hello"}\n')
            stream.writestr(prefix + 'train/clip.wav', b'fixture audio')
        return archive

    def test_symlink_root_is_rejected_before_external_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); outside = root / 'outside'; outside.mkdir()
            (outside / 'keep.txt').write_bytes(b'keep')
            destination = root / 'dataset'; destination.symlink_to(outside, target_is_directory=True)
            archive = self.make_zip(root)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                training.extract_zip(str(archive), str(destination))
            self.assertEqual(['keep.txt'], sorted(p.name for p in outside.iterdir()))
            self.assertEqual(b'keep', (outside / 'keep.txt').read_bytes())
            self.assertTrue(destination.is_symlink())

    def test_unreferenced_and_internal_symlinks_are_rejected_before_any_archive_write(self):
        for mode in ('external-directory', 'internal-file', 'dangling'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); dest = root / 'dest'; dest.mkdir()
                outside = root / 'outside'; outside.mkdir()
                (outside / 'metadata.jsonl').write_bytes(b'external metadata')
                (dest / 'keep.txt').write_bytes(b'keep')
                target = {'external-directory': outside, 'internal-file': dest / 'keep.txt',
                          'dangling': root / 'absent'}[mode]
                (dest / 'unreferenced').symlink_to(target, target_is_directory=mode == 'external-directory')
                archive = self.make_zip(root, nested=True)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    training.extract_zip(str(archive), str(dest))
                self.assertEqual(['keep.txt', 'unreferenced'], sorted(p.name for p in dest.iterdir()))
                self.assertEqual(b'keep', (dest / 'keep.txt').read_bytes())
                self.assertEqual(b'external metadata', (outside / 'metadata.jsonl').read_bytes())

    def test_regular_existing_destination_replaces_and_flattens_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); dest = root / 'dest'; dest.mkdir()
            (dest / 'keep.txt').write_bytes(b'keep')
            archive = self.make_zip(root, nested=True)
            training.extract_zip(str(archive), str(dest))
            self.assertEqual('{"text":"hello"}\n', (dest / 'metadata.jsonl').read_text())
            self.assertEqual(b'fixture audio', (dest / 'train/clip.wav').read_bytes())
            self.assertFalse((dest / 'keep.txt').exists())
            self.assertFalse((dest / 'dataset').exists())

    def test_existing_member_level_symlink_escape_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); dest = root / 'dest'; dest.mkdir()
            outside = root / 'outside'; outside.mkdir()
            (dest / 'train').symlink_to(outside, target_is_directory=True)
            archive = self.make_zip(root)
            with self.assertRaises(ValueError):
                training.extract_zip(str(archive), str(dest))
            self.assertFalse((outside / 'clip.wav').exists())
