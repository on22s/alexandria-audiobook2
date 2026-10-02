import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
spec = importlib.util.spec_from_file_location(
    'alexandria_run_manifest', ROOT / 'alexandria_run_manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class PreparerRunManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'book.wav'
        self.audio.write_bytes(b'audio A')
        self.source = self.root / 'book.txt'
        self.source.write_text('edition A')
        self.args = SimpleNamespace(
            audio=str(self.audio), source=str(self.source), chunk_size=10.0,
            phase=None, resume=False, hf_token='secret', output='book.zip')
        self.work = self.root / 'dataset_temp'

    def test_real_phase_borrows_lock_without_releasing_parent_ownership(self):
        import subprocess
        descriptor = manifest.acquire_run_lock(self.work)
        code = "from alexandria_run_manifest import acquire_run_lock;import os,sys;fd=acquire_run_lock(sys.argv[1]);os.close(fd)"
        try:
            with patch.dict(os.environ, {'PYTHONPATH': str(ROOT)}):
                result = manifest.run_phase_with_lock([sys.executable, '-c', code, str(self.work)], descriptor)
            self.assertEqual(0, result.returncode)
            with self.assertRaises(manifest.RunStateError):
                manifest.acquire_run_lock(self.work)
        finally:
            os.close(descriptor)
        released = manifest.acquire_run_lock(self.work)
        os.close(released)

    def test_content_and_effective_options_identify_one_run(self):
        first = manifest.get_run_identity(self.args)
        self.assertNotIn('secret', json.dumps(first))
        manifest.ensure_run_manifest(self.work, first, fresh=True)
        manifest.ensure_run_manifest(self.work, first, fresh=False)
        for path, data in ((self.audio, b'audio B'),
                           (self.source, b'edition B')):
            with self.subTest(path=path):
                old = path.read_bytes()
                path.write_bytes(data)
                with self.assertRaisesRegex(manifest.RunStateError, 'changed'):
                    manifest.ensure_run_manifest(
                        self.work, manifest.get_run_identity(self.args), fresh=False)
                path.write_bytes(old)
        self.args.chunk_size = 30.0
        with self.assertRaisesRegex(manifest.RunStateError, 'changed'):
            manifest.ensure_run_manifest(
                self.work, manifest.get_run_identity(self.args), fresh=False)

    def test_fresh_run_clears_generated_artifacts_but_not_unrelated_files(self):
        self.work.mkdir()
        (self.work / 'asr_segments.json').write_text('old')
        (self.work / 'enriched_segments.json').write_text('old')
        (self.work / 'diarization.json').write_text('old')
        (self.work / 'sample_0000.wav').write_bytes(b'old')
        (self.work / 'unrelated.txt').write_text('keep')
        manifest.ensure_run_manifest(
            self.work, manifest.get_run_identity(self.args), fresh=True)
        for name in ('asr_segments.json', 'enriched_segments.json',
                     'diarization.json', 'sample_0000.wav'):
            self.assertFalse((self.work / name).exists())
        self.assertEqual('keep', (self.work / 'unrelated.txt').read_text())

    def test_only_complete_valid_artifacts_are_reused(self):
        identity = manifest.get_run_identity(self.args)
        manifest.ensure_run_manifest(self.work, identity, fresh=True)
        asr = self.work / 'asr_segments.json'
        asr.write_text('{"detected_lang":"en","word_segments":[{"word":"hello"}]}')
        self.assertFalse(manifest.is_verified_artifact(self.work, identity, 'asr', asr))
        manifest.mark_artifact_complete(self.work, identity, 'asr', asr)
        self.assertTrue(manifest.is_verified_artifact(self.work, identity, 'asr', asr))
        asr.write_text('{"word_segments":')
        self.assertFalse(manifest.is_verified_artifact(self.work, identity, 'asr', asr))
        asr.write_text('{"detected_lang":"en","word_segments":[]}')
        self.assertFalse(manifest.is_verified_artifact(self.work, identity, 'asr', asr))
        enriched = self.work / 'enriched_segments.json'
        enriched.write_text('[{"words":[]}]')
        with self.assertRaisesRegex(manifest.RunStateError, 'Invalid enriched'):
            manifest.mark_artifact_complete(self.work, identity, 'enriched', enriched)

    def test_failed_atomic_write_preserves_old_artifact(self):
        target = self.root / 'artifact.json'
        target.write_text('{"complete":true}')
        with self.assertRaises(TypeError):
            manifest.write_json_atomic({'bad': object()}, target)
        self.assertEqual('{"complete":true}', target.read_text())

    def test_symlinked_work_directory_is_refused(self):
        target = self.root / 'other'
        target.mkdir()
        self.work.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(manifest.RunStateError, 'symlink'):
            manifest.ensure_run_manifest(
                self.work, manifest.get_run_identity(self.args), fresh=True)
        self.assertEqual([], list(target.iterdir()))

    def test_one_run_owns_the_shared_directory_lock(self):
        first = manifest.acquire_run_lock(self.work)
        try:
            with self.assertRaisesRegex(manifest.RunStateError, 'Another preparer run'):
                manifest.acquire_run_lock(self.work)
            with patch.dict(os.environ, {manifest.LOCK_ENV: str(first)}):
                self.assertEqual(first, manifest.acquire_run_lock(self.work))
        finally:
            os.close(first)

    def test_success_cleanup_keeps_unrelated_work_directory_files(self):
        identity = manifest.get_run_identity(self.args)
        manifest.ensure_run_manifest(self.work, identity, fresh=True)
        (self.work / 'sample_0000.wav').write_bytes(b'generated')
        (self.work / 'asr_segments.json').write_text('generated')
        (self.work / 'unrelated.txt').write_text('keep')
        manifest.cleanup_run_artifacts(self.work, identity)
        self.assertTrue(self.work.is_dir())
        self.assertEqual('keep', (self.work / 'unrelated.txt').read_text())
        self.assertFalse((self.work / 'sample_0000.wav').exists())

    def test_scratch_path_refuses_existing_input_and_dangling_symlink(self):
        identity = manifest.get_run_identity(self.args)
        manifest.ensure_run_manifest(self.work, identity, fresh=True)
        with self.assertRaisesRegex(manifest.RunStateError, 'not owned'):
            manifest.validate_scratch_path(self.work, identity, self.audio)
        scratch = self.root / 'custom-scratch.wav'
        scratch.symlink_to(self.root / 'missing.wav')
        with self.assertRaisesRegex(manifest.RunStateError, 'not owned'):
            manifest.validate_scratch_path(self.work, identity, scratch)
        scratch.unlink()
        manifest.validate_scratch_path(self.work, identity, scratch)
        scratch.write_bytes(b'generated')
        manifest.mark_artifact_complete(self.work, identity, 'scratch', scratch)
        manifest.validate_scratch_path(self.work, identity, scratch)


if __name__ == '__main__':
    unittest.main()
