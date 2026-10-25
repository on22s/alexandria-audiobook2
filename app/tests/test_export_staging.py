"""Ownership-marked export staging survives ordinary exit and reclaims only abandoned work."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from export_staging import (ExportStaging, create_export_staging, reclaim_export_staging,
                            MARKER, PREFIXES)


APP = Path(__file__).resolve().parent.parent
CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
staging = create_export_staging(sys.argv[2], sys.argv[3])
(staging.path / 'payload.bin').write_bytes(b'x' * 4096)
print(staging.path, flush=True)
if sys.argv[4] == 'recovery':
    (staging.path / 'old').mkdir()
    (staging.path / 'old' / 'backup.wav').write_bytes(b'keep')
    os._exit(76)
elif sys.argv[4] == 'hold':
    time.sleep(60)
else:
    os._exit(76)
'''


class ExportStagingTests(unittest.TestCase):
    def run_child(self, parent, kind='mp3', mode='hold'):
        process = subprocess.Popen(
            [sys.executable, '-c', CHILD, str(APP), str(parent), kind, mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = process.stdout.readline().strip()
        if not line:
            error = process.stderr.read()
            process.kill()
            process.wait(timeout=10)
            self.fail(error or 'child did not report a staging path')
        return process, Path(line)

    def test_normal_exit_removes_the_marked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with create_export_staging(tmp, 'mp3') as staging:
                path = staging.path
                (path / 'payload.bin').write_bytes(b'x')
                self.assertTrue(path.is_dir())
            self.assertFalse(path.exists())

    def test_hard_exit_leaves_a_verifiably_abandoned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, mode='exit')
            process.wait(timeout=10)
            process.communicate()
            self.assertEqual(76, process.returncode)
            self.assertTrue(path.is_dir())
            result = reclaim_export_staging((tmp,))
            self.assertEqual([str(path)], result['removed'])
            self.assertFalse(path.exists())

    def test_live_owner_is_preserved_until_the_lock_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, mode='hold')
            try:
                result = reclaim_export_staging((tmp,))
                self.assertEqual([], result['removed'])
                self.assertEqual([str(path)], result['preserved'])
                self.assertTrue(path.is_dir())
            finally:
                process.kill()
                process.wait(timeout=10)
                process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual([str(path)], result['removed'])
            self.assertFalse(path.exists())

    def test_parallel_live_owners_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, first_path = self.run_child(tmp, mode='hold')
            second, second_path = self.run_child(tmp, mode='hold')
            try:
                result = reclaim_export_staging((tmp,))
                self.assertEqual([], result['removed'])
                self.assertEqual({str(first_path), str(second_path)},
                                 set(result['preserved']))
                self.assertTrue(first_path.is_dir())
                self.assertTrue(second_path.is_dir())
            finally:
                for process in (first, second):
                    process.kill()
                    process.wait(timeout=10)
                    process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual({str(first_path), str(second_path)},
                             set(result['removed']))

    def test_longer_staging_prefixes_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ('cover', 'progress'):
                with self.subTest(kind=kind):
                    process, path = self.run_child(tmp, kind=kind, mode='exit')
                    process.wait(timeout=10)
                    process.communicate()
                    result = reclaim_export_staging((tmp,))
                    self.assertIn(str(path), result['removed'])
                    self.assertFalse(path.exists())

    def test_corrupt_marker_is_preserved_without_aborting_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / (PREFIXES['mp3'] + 'broken')
            broken.mkdir()
            (broken / MARKER).write_text('[' * 2000 + ']' * 2000)
            result = reclaim_export_staging((root,))
            self.assertEqual([], result['removed'])
            self.assertIn(str(broken), result['preserved'])
            self.assertTrue(broken.is_dir())

    def test_chapter_rollback_old_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            process, path = self.run_child(tmp, kind='chapter', mode='recovery')
            process.wait(timeout=10)
            process.communicate()
            result = reclaim_export_staging((tmp,))
            self.assertEqual([], result['removed'])
            self.assertIn(str(path), result['preserved'])
            self.assertTrue((path / 'old' / 'backup.wav').is_file())

    def test_unknown_sibling_missing_marker_and_foreign_parent_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sibling = root / '.mp3-merge-not-a-staging-dir'
            sibling.mkdir()
            (sibling / 'unknown.bin').write_bytes(b'keep')
            missing = root / (PREFIXES['mp3'] + 'missing-marker')
            missing.mkdir()
            (missing / 'payload.bin').write_bytes(b'keep')
            foreign = root / (PREFIXES['mp3'] + 'foreign-parent')
            foreign.mkdir()
            (foreign / MARKER).write_text(
                '{"schema":1,"kind":"mp3","prefix":".mp3-merge-",'
                '"parent":"/definitely/not/this/root","token":"' + '0' * 32 + '"}')
            symlink = root / (PREFIXES['mp3'] + 'symlink')
            symlink.symlink_to(sibling, target_is_directory=True)
            result = reclaim_export_staging((root,))
            self.assertEqual([], result['removed'])
            self.assertTrue((sibling / 'unknown.bin').is_file())
            self.assertTrue((missing / 'payload.bin').is_file())
            self.assertTrue((foreign / MARKER).is_file())
            self.assertTrue(symlink.is_symlink())

    def test_preserved_recovery_directory_is_not_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = create_export_staging(tmp, 'chapter')
            path = staging.path
            (path / 'recovery.bin').write_bytes(b'keep')
            staging.finish(remove=False)
            result = reclaim_export_staging((tmp,))
            self.assertEqual([], result['removed'])
            self.assertTrue((path / 'recovery.bin').is_file())


if __name__ == '__main__':
    unittest.main()
