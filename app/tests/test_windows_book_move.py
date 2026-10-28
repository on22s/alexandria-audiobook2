"""Parent-directory admission for draft Windows book moves."""
import os
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import windows_export_staging_directory as windows
import book_state_transaction as books


class FakeHandle:
    opened = []
    def __init__(self, _api, path):
        self.path = Path(path)
        self.closed = False
        self.opened.append(self)

    def information(self):
        info = self.path.lstat()
        return SimpleNamespace(attributes=0x400 if self.path.is_symlink() else 0x10,
            volume=info.st_dev, index_high=info.st_ino >> 32,
            index_low=info.st_ino & 0xffffffff)

    def close(self):
        self.closed = True


class WindowsBookMoveGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Requires native Windows cleanup handles')
    def test_recovery_guard_blocks_metadata_unlink_and_directory_removal(self):
        from export_staging_directory import OWNER_FILE
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)/'stage'; stage.mkdir()
            (stage/OWNER_FILE).write_bytes(b'owner')
            (stage/'.export-staging.json').write_bytes(b'{}')
            info = stage.lstat()
            with windows.WindowsStageDirectory(stage, (info.st_dev, info.st_ino)) as guarded:
                guarded.create_recovery_guard('.export-staging-recovery')
                for name in ('.export-staging-recovery', OWNER_FILE, '.export-staging.json'):
                    with self.subTest(name=name), self.assertRaises(windows.StageDirectoryChanged):
                        guarded.unlink(name)
                    self.assertTrue((stage/name).exists())
                with self.assertRaises(windows.StageDirectoryChanged):
                    guarded.remove_empty_directory()

    @unittest.skipUnless(os.name == 'nt', 'Requires native Windows cleanup handles')
    def test_native_byte_and_stream_transactions_finish_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'artifact.bin'
            target.write_bytes(b'old')
            with books.ensure_book_state(root):
                books.apply_book_state_locked(root, {'artifact.bin': b'byte replacement'}, [])
            self.assertEqual(b'byte replacement', target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

            raw = b'stream replacement'
            source = books.BookStateStream(io.BytesIO(raw), len(raw),
                                           hashlib.sha256(raw).hexdigest())
            with books.ensure_book_state(root):
                books.apply_book_state_locked(root, {'artifact.bin': source}, [],
                                              stream_byte_limit=1024)
            self.assertEqual(raw, target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    def test_guard_holds_all_ancestors_once_and_releases_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            (root / 'source').mkdir()
            (root / 'destination').mkdir()
            FakeHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', FakeHandle):
                with windows.guard_windows_book_move_parents(
                        root, root / 'source/file', root / 'destination/file'):
                    paths = [handle.path for handle in FakeHandle.opened]
                    self.assertIn(root, paths)
                    self.assertIn(root / 'source', paths)
                    self.assertIn(root / 'destination', paths)
                    self.assertEqual(len(paths), len(set(paths)))
                    self.assertTrue(all(not handle.closed for handle in FakeHandle.opened))
                self.assertTrue(all(handle.closed for handle in FakeHandle.opened))

    def test_untrusted_outside_target_is_refused_before_native_access(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            with patch.object(windows, '_api', side_effect=AssertionError('unexpected native access')):
                with self.assertRaisesRegex(ValueError, 'escapes'):
                    with windows.guard_windows_book_move_parents(root, root / 'source', Path(outside) / 'dest'):
                        self.fail('Outside path admitted')

    @unittest.skipIf(os.name == 'nt', 'Windows symlink creation may need developer privileges')
    def test_reparse_ancestor_refuses_and_releases_prior_handles(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / 'redirect').symlink_to(outside, target_is_directory=True)
            FakeHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', FakeHandle):
                with self.assertRaisesRegex(windows.StageDirectoryChanged, 'Unsafe'):
                    with windows.guard_windows_book_move_parents(
                            root, root / 'redirect/source', root / 'destination'):
                        self.fail('Reparse point was admitted')
            self.assertTrue(all(handle.closed for handle in FakeHandle.opened))

    @unittest.skipUnless(os.name == 'nt', 'Requires native Windows sharing semantics')
    def test_native_handles_deny_parent_rename_until_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            parent = root / 'parent'
            parent.mkdir()
            with windows.guard_windows_book_move_parents(root, parent / 'source', parent / 'destination'):
                with self.assertRaises(OSError):
                    parent.rename(root / 'moved')
            parent.rename(root / 'moved')
            self.assertTrue((root / 'moved').is_dir())
