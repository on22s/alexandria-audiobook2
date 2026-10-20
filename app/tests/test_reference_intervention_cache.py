"""Actual intervention CLI refuses stale training inputs before another train."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import zipfile

from experiments import reference_intervention as runner


class ReferenceInterventionCacheTests(unittest.TestCase):
    def write_zip(self, path, version):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(bytes((version, 16)) * 2400)
        rows = [{'audio_filepath': f'audio/{i}.wav', 'text': f'version {version} line {i}'}
                for i in range(2)]
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('metadata.jsonl', '\n'.join(json.dumps(row) for row in rows))
            for i in range(2):
                archive.writestr(f'audio/{i}.wav', audio.getvalue())

    def test_changed_zip_refuses_base_and_arm_reuse_without_training_or_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); zips = root / 'zips'; zips.mkdir()
            models = root / 'models'; adapter = models / 'fixture'; adapter.mkdir(parents=True)
            (adapter / 'training_meta.json').write_text(json.dumps({'ref_sample_audio': 'fixture_dataset/ref.wav'}))
            (root / 'ab_test_runtime/logs').mkdir(parents=True)
            source = zips / 'fixture_dataset.zip'; self.write_zip(source, 1)
            work = root / 'work'; out = root / 'result.json'; trained = []
            def train(command, **kwargs):
                directory = Path(command[command.index('--data_dir') + 1])
                trained.append((directory.name, (directory / 'metadata.jsonl').read_bytes(),
                                (directory / 'audio/0.wav').read_bytes()))
                return SimpleNamespace(returncode=1)
            args = [runner.__file__, '--adapter', 'fixture', '--models', str(models), '--zips', str(zips),
                    '--work', str(work), '--out', str(out), '--rank-clips', '2']
            with patch.object(runner, 'REPO', tmp), patch.object(sys, 'argv', args), \
                    patch.object(runner, 'rank_clips', return_value=(0, 1, {0: .8, 1: .2})), \
                    patch.object(runner.subprocess, 'run', side_effect=train), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as first:
                    runner.main()
                self.assertEqual(3, first.exception.code)  # Both synthetic training calls fail.
                self.assertEqual(['data_medoid', 'data_worst'], [row[0] for row in trained])
                self.assertTrue(all(b'version 1' in row[1] for row in trained))
                with patch.object(runner.shutil, 'copytree', side_effect=AssertionError('unchanged cache copied again')):
                    with self.assertRaises(SystemExit) as repeat:
                        runner.main()
                    self.assertEqual(3, repeat.exception.code)
                self.assertEqual(trained[:2], trained[2:])
                previous = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob('*') if p.is_file()}
                previous_result = out.read_bytes()
                self.write_zip(source, 2)
                with self.assertRaisesRegex(SystemExit, 'identity|fresh.*work'):
                    runner.main()
                self.assertEqual(4, len(trained))
                self.assertEqual(previous_result, out.read_bytes())
                self.assertEqual(previous, {str(p.relative_to(work)): p.read_bytes()
                                           for p in work.rglob('*') if p.is_file()})

    def test_legacy_and_mutated_cache_refuse_without_replacing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'source.zip'; self.write_zip(archive, 1)
            legacy = root / 'legacy'; legacy.mkdir(); (legacy / 'metadata.jsonl').write_text('old input')
            with self.assertRaisesRegex(SystemExit, 'identity'):
                runner.ensure_reference_data_cache(str(archive), legacy)
            self.assertEqual('old input', (legacy / 'metadata.jsonl').read_text())
            base = root / 'data'
            runner.ensure_reference_data_cache(str(archive), base)
            (base / 'audio/0.wav').write_bytes(b'externally replaced audio')
            previous = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
            with self.assertRaisesRegex(SystemExit, 'identity'):
                runner.ensure_reference_data_cache(str(archive), base)
            self.assertEqual(previous, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()})
            arm = root / 'data_medoid'
            with self.assertRaisesRegex(SystemExit, 'identity'):
                runner.ensure_reference_data_cache(str(archive), arm, base)
            self.assertFalse(arm.exists())

    def test_failed_extraction_and_arm_copy_publish_no_partial_cache_and_allow_retry(self):
        import retrain_honest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'source.zip'; self.write_zip(archive, 1)
            base = root / 'data'
            def partial_extract(source, destination):
                path = Path(destination); path.mkdir(); (path / 'metadata.jsonl').write_text('partial')
                raise OSError('synthetic extraction failure')
            with patch.object(retrain_honest, 'extract', side_effect=partial_extract):
                with self.assertRaisesRegex(OSError, 'extraction'):
                    runner.ensure_reference_data_cache(str(archive), base)
            self.assertFalse(base.exists())
            self.assertEqual([], list(root.glob('.reference-data-*')))
            runner.ensure_reference_data_cache(str(archive), base)
            arm = root / 'data_medoid'
            copy = runner.shutil.copytree
            def partial_copy(source, destination):
                with patch.object(runner.shutil, 'copytree', copy):
                    copy(source, destination)
                raise OSError('synthetic copy failure')
            with patch.object(runner.shutil, 'copytree', side_effect=partial_copy):
                with self.assertRaisesRegex(OSError, 'copy'):
                    runner.ensure_reference_data_cache(str(archive), arm, base)
            self.assertFalse(arm.exists())
            self.assertEqual([], list(root.glob('.reference-data-*')))
            runner.ensure_reference_data_cache(str(archive), arm, base)
            self.assertEqual((base / 'audio/0.wav').read_bytes(), (arm / 'audio/0.wav').read_bytes())
