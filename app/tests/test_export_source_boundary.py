import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import export_benchmark
from tests import test_export_benchmark as fixtures


class ExportSourceBoundaryTests(unittest.TestCase):
    def test_worker_rejects_outside_or_nonfile_audio_before_copy_and_export(self):
        for mode in ('parent', 'absolute', 'external-link', 'directory', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); fixture_root = root / 'fixture'; fixture_root.mkdir()
                payload = fixtures.ExportBenchmarkSourceHashTests().make_payload(fixture_root)
                outside = root / 'private.wav'; outside.write_bytes((fixture_root / 'sample-0.wav').read_bytes())
                (fixture_root / 'outside.wav').symlink_to(outside)
                (fixture_root / 'folder.wav').mkdir()
                path = {'parent': '../private.wav', 'absolute': str(outside),
                        'external-link': 'outside.wav', 'directory': 'folder.wav', 'missing': 'missing.wav'}[mode]
                payload['fixture']['chunks'][0]['audio_path'] = path
                payload['fixture']['audio_sha256'][path] = hashlib.sha256(outside.read_bytes()).hexdigest()
                original = outside.read_bytes(); before = copy.deepcopy(payload)
                with patch.object(export_benchmark.shutil, 'copy2', wraps=export_benchmark.shutil.copy2) as copier, \
                        patch.object(export_benchmark, 'ProjectManager', wraps=export_benchmark.ProjectManager) as manager:
                    with self.assertRaisesRegex(ValueError, 'outside.*missing'):
                        export_benchmark.execute_payload(payload)
                    copier.assert_not_called(); manager.assert_not_called()
                self.assertEqual(original, outside.read_bytes())
                self.assertEqual(before, payload)

    def test_contained_absolute_and_symlink_sources_still_produce_actual_audacity_zip(self):
        for mode in ('absolute', 'internal-link'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); payload = fixtures.ExportBenchmarkSourceHashTests().make_payload(root)
                source = root / 'sample-0.wav'; original = source.read_bytes()
                (root / 'inside.wav').symlink_to(source)
                path = str(source) if mode == 'absolute' else 'inside.wav'
                payload['fixture']['chunks'][0]['audio_path'] = path
                payload['fixture']['audio_sha256'][path] = hashlib.sha256(original).hexdigest()
                before = copy.deepcopy(payload)
                result = export_benchmark.execute_payload(payload)
                self.assertEqual('passed', result['status'])
                self.assertEqual(2, result['label_count'])
                self.assertIn('project.lof', result['members'])
                self.assertGreater(result['artifact_bytes'], 0)
                self.assertEqual(original, source.read_bytes())
                self.assertEqual(before, payload)
