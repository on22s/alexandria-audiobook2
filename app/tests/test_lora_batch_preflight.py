"""A broken late archive must prevent all training subprocesses."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('batch_preflight_test', ROOT / 'tools/voice_lab/batch_train_lora.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)

def save_valid_training_zip(path):
    """Write a native PCM/metadata dataset for batch-admission fixtures."""
    pcm = io.BytesIO()
    with wave.open(pcm, 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00' * 2400)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('metadata.jsonl', '{"audio_filepath":"clip.wav","text":"Hello."}\n')
        archive.writestr('clip.wav', pcm.getvalue())

def apply_test_training_dependency_fixture(testcase):
    """Supply discovery-only Qwen package for CPU scheduling tests, not inference."""
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    Path(temporary.name, 'qwen_tts.py').write_text(
        "raise RuntimeError('This test fixture cannot perform Qwen inference')\n")
    search = temporary.name + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
    environment = patch.dict(os.environ, {'PYTHONPATH': search})
    environment.start()
    testcase.addCleanup(environment.stop)


class BatchPreflightTests(unittest.TestCase):
    def setUp(self):
        apply_test_training_dependency_fixture(self)

    def test_damaged_last_zip_refuses_entire_dry_and_full_batch(self):
        for dry in (False, True):
            with self.subTest(dry=dry), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                zips = root / 'zips'
                zips.mkdir()
                pcm = io.BytesIO()
                with wave.open(pcm, 'wb') as audio:
                    audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(b'\x01\x00' * 2400)
                with zipfile.ZipFile(zips / 'a_good.zip', 'w') as archive:
                    archive.writestr('metadata.jsonl', json.dumps({'audio_filepath': 'clip.wav', 'text': 'Hello.'}) + '\n')
                    archive.writestr('clip.wav', pcm.getvalue())
                (zips / 'z_bad.zip').write_bytes(b'not a zip')
                before = {p.name: p.read_bytes() for p in zips.iterdir()}
                argv = ['batch', '--zips_dir', str(zips), '--models_dir', str(root / 'models'), '--datasets_dir', str(root / 'datasets'), '--manifest', str(root / 'manifest.json'), '--device', 'cpu', '--python', sys.executable]
                if dry:
                    argv.append('--dry_run')
                output = io.StringIO()
                with patch.object(sys, 'argv', argv), patch.object(batch, 'train_one', return_value=None) as train, contextlib.redirect_stdout(output):
                    self.assertEqual(1, batch.main())
                train.assert_not_called()
                self.assertIn('z_bad.zip', output.getvalue())
                self.assertFalse((root / 'models').exists())
                self.assertFalse((root / 'datasets').exists())
                self.assertEqual(before, {p.name: p.read_bytes() for p in zips.iterdir()})

    def test_all_bad_archives_are_reported_and_temp_inputs_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            cases = [('bad_shape', {'audio_filepath': 'clip.wav', 'text': 1}),
                     ('missing_reference', {'audio_filepath': 'clip.wav', 'text': 'hi', 'ref_audio': 'missing.wav'}),
                     ('escaping_audio', {'audio_filepath': '../outside.wav', 'text': 'hi'}),
                     ('corrupt_audio', {'audio_filepath': 'clip.wav', 'text': 'hi'})]
            for name, row in cases:
                path = root / (name + '.zip')
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('metadata.jsonl', json.dumps(row) + '\n')
                    archive.writestr('clip.wav', b'not audio')
                paths.append(str(path))
            before = {path: Path(path).read_bytes() for path in paths}
            report = batch.get_batch_archive_preflight(paths)
            self.assertEqual([], report['datasets'])
            self.assertEqual({Path(path).name for path in paths}, {row['archive'] for row in report['errors']})
            self.assertEqual(before, {path: Path(path).read_bytes() for path in paths})

    def test_output_access_and_disk_fail_without_creating_paths(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(datasets_dir=str(root / 'datasets'), models_dir=str(root / 'models'), manifest=str(root / 'manifest.json'), keep_datasets=False)
            with patch.object(batch.os, 'access', return_value=False):
                errors = batch.get_batch_output_preflight(args, [])
            self.assertEqual(4, len(errors))
            with patch.object(batch.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
                errors = batch.get_batch_output_preflight(args, [])
            self.assertEqual(4, len(errors))
            self.assertEqual([], list(root.iterdir()))

    def test_native_training_split_reference_and_nonfinite_audio(self):
        import numpy as np
        import soundfile as sf
        from dataset_metadata import get_training_dataset_preflight
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'dataset'
            root.mkdir()
            sf.write(Path(tmp) / 'outside.wav', np.ones(2400) * .1, 24000)
            (root / 'train').mkdir()
            sf.write(root / 'clip.wav', np.ones(2400) * .1, 24000)
            sf.write(root / 'ref.wav', np.ones(2400) * .2, 24000)
            # Broken root metadata is ignored when the explicit train split exists.
            (root / 'metadata.jsonl').write_text('broken root\n')
            metadata = root / 'train/metadata.jsonl'
            metadata.write_text(json.dumps({'audio_filepath': 'clip.wav', 'text': 'Hello.'}) + '\n')
            report = get_training_dataset_preflight(str(root))
            self.assertTrue(report['used_split'])
            self.assertEqual('ref.wav', report['reference'])
            metadata.write_text(json.dumps({'audio_filepath': 'clip.wav', 'text': 'Hello.', 'ref_audio': '../outside.wav'}) + '\n')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                get_training_dataset_preflight(str(root))
            metadata.write_text(json.dumps({'audio_filepath': 'clip.wav', 'text': 'Hello.'}) + '\n')
            sf.write(root / 'ref.wav', np.array([.1, float('nan')]), 24000, subtype='FLOAT')
            with self.assertRaisesRegex(ValueError, 'non-finite'):
                get_training_dataset_preflight(str(root))

    def test_valid_cpu_dry_run_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zips = root / 'zips'
            zips.mkdir()
            pcm = io.BytesIO()
            with wave.open(pcm, 'wb') as audio:
                audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\x01\x00' * 2400)
            with zipfile.ZipFile(zips / 'valid.zip', 'w') as archive:
                archive.writestr('metadata.jsonl', '{"audio_filepath":"clip.wav","text":"Hello."}\n')
                archive.writestr('clip.wav', pcm.getvalue())
            argv = ['batch', '--zips_dir', str(zips), '--models_dir', str(root / 'models'), '--datasets_dir', str(root / 'datasets'), '--manifest', str(root / 'manifest.json'), '--device', 'cpu', '--python', sys.executable, '--dry_run']
            (root / 'models').mkdir()
            (root / 'models/manifest.json').write_text('[]')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(sys, 'argv', argv), patch.object(batch, 'train_one') as train, patch.object(batch, 'adapter_exists') as resume, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, batch.main())
            train.assert_not_called()
            resume.assert_not_called()
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertFalse((root / 'datasets').exists())
