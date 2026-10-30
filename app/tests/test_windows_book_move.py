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
from tests.test_support import use_resolved_tempdir


class FakeHandle:
    opened = []
    def __init__(self, _api, path, **options):
        self.path = Path(path)
        self.options = options
        self.closed = False
        self.opened.append(self)

    def information(self):
        info = self.path.lstat()
        attributes = 0x400 if self.path.is_symlink() else 0x10 if self.path.is_dir() else 0
        return SimpleNamespace(attributes=attributes,
            volume=info.st_dev, index_high=info.st_ino >> 32,
            index_low=info.st_ino & 0xffffffff, links=info.st_nlink)

    def close(self):
        self.closed = True


class WindowsBookMoveGuardTests(unittest.TestCase):
    def setUp(self):
        use_resolved_tempdir(self)

    def test_named_file_identity_uses_and_releases_native_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'staged'
            path.write_bytes(b'owned')
            info = path.lstat()
            FakeHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', FakeHandle):
                self.assertEqual((info.st_dev, info.st_ino),
                                 windows.windows_named_file_identity(path))
            self.assertEqual([path], [handle.path for handle in FakeHandle.opened])
            self.assertTrue(FakeHandle.opened[0].closed)

    def test_named_file_identity_rejects_native_multi_link_file(self):
        class MultiLinkHandle(FakeHandle):
            def information(self):
                info = super().information()
                info.links = 2
                return info
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'staged'
            path.write_bytes(b'owned')
            MultiLinkHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', MultiLinkHandle):
                with self.assertRaisesRegex(windows.StageDirectoryChanged,
                                            'Unsafe Windows book staging file'):
                    windows.windows_named_file_identity(path)
            self.assertTrue(MultiLinkHandle.opened[0].closed)

    def test_system_ancestor_access_denial_does_not_weaken_owned_root(self):
        for code in (5, 32):
            with self.subTest(winerror=code):
                denied = OSError('synthetic unavailable rename authority')
                denied.winerror = code
                fallback = object()
                with patch.object(windows, '_Handle',
                                  side_effect=[denied, fallback]) as opened:
                    self.assertIs(fallback, windows._open_rename_guard(
                        object(), Path('system-ancestor'), required=False))
                self.assertEqual({}, opened.call_args_list[1].kwargs)
        denied = OSError('synthetic access denial')
        denied.winerror = 5
        with patch.object(windows, '_Handle', side_effect=denied) as opened:
            with self.assertRaises(OSError):
                windows._open_rename_guard(object(), Path('owned-root'), required=True)
        self.assertEqual({'protect_rename': True}, opened.call_args.kwargs)

    def test_book_root_guard_uses_non_deleting_handles_and_checks_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            identity = root.lstat()
            FakeHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', FakeHandle):
                with windows.WindowsBookRoot(root, (identity.st_dev, identity.st_ino)) as guard:
                    guard.check()
                    self.assertEqual(root, FakeHandle.opened[-1].path)
                    self.assertEqual({'protect_rename': True}, FakeHandle.opened[-1].options)
                    self.assertTrue(all(not handle.closed for handle in FakeHandle.opened))
                self.assertTrue(all(handle.closed for handle in FakeHandle.opened))

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
                    by_path = {handle.path: handle.options for handle in FakeHandle.opened}
                    self.assertEqual({'protect_rename': True}, by_path[root])
                    self.assertEqual({'protect_rename': True}, by_path[root / 'source'])
                    self.assertEqual({'protect_rename': True}, by_path[root / 'destination'])
                    self.assertTrue(all(not handle.closed for handle in FakeHandle.opened))
                self.assertTrue(all(handle.closed for handle in FakeHandle.opened))

    def test_move_guard_reuses_an_admitted_root_without_reopening_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            parent = root / 'parent'
            parent.mkdir(parents=True)
            identity = root.lstat()
            FakeHandle.opened = []
            with patch.object(windows, '_api', return_value=object()), \
                    patch.object(windows, '_Handle', FakeHandle):
                with windows.WindowsBookRoot(
                        root, (identity.st_dev, identity.st_ino)) as admitted:
                    root_handles = [handle for handle in FakeHandle.opened
                                    if handle.path == root]
                    with windows.guard_windows_book_move_parents(
                            root, parent / 'source', parent / 'destination',
                            root_guard=admitted):
                        self.assertEqual(root_handles, [handle for handle in FakeHandle.opened
                                                       if handle.path == root])
                        self.assertEqual(1, len([handle for handle in FakeHandle.opened
                                                if handle.path == parent]))

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

