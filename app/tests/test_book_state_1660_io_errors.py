"""#1660 (b): an artifact or journal that cannot be read is the module's
refusal (ValueError, original error as the cause), as the byte digest and
_move already report it; "not supported" stays an OSError."""
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books


class BookIoErrorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.journal = self.root / books.JOURNAL
        self.journal.write_text(json.dumps({'version': 3, 'phase': 'publishing'}), encoding='utf-8')

    def assert_refused(self, call, cause):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertIsInstance(caught.exception.__cause__, cause)
        return caught.exception

    def test_unreadable_journal_is_a_refusal_and_is_left_alone(self):
        before = self.journal.read_bytes()
        version, opened = books._book_target_version, os.open
        def failing_lstat(path):
            if Path(path).name == books.JOURNAL:
                raise PermissionError(errno.EACCES, 'fixture')
            return version(path)
        def failing_open(path, *args, **kwargs):
            if Path(path).name == books.JOURNAL:
                raise PermissionError(errno.EACCES, 'fixture')
            return opened(path, *args, **kwargs)
        for label, target, replacement in (('lstat', '_book_target_version', failing_lstat),
                                           ('open', 'os', None)):
            with self.subTest(label):
                context = (patch.object(books, target, side_effect=replacement) if replacement
                           else patch.object(books.os, 'open', side_effect=failing_open))
                with context:
                    error = self.assert_refused(lambda: books._read_book_journal(self.journal),
                                                PermissionError)
                    self.assertIn('journal could not be read', str(error))
                    self.assert_refused(lambda: books.ensure_book_state(self.root).__enter__(),
                                        PermissionError)
                self.assertEqual(before, self.journal.read_bytes())

    def test_scan_io_error_is_a_refusal_but_unsupported_stays_oserror(self):
        artifact = self.root / 'audio.wav'
        artifact.write_bytes(b'bytes')
        budget = lambda: books._BookScanBudget(1024, 8, None)
        with patch.object(books, '_book_target_version', side_effect=OSError(errno.EIO, 'fixture')):
            self.assert_refused(lambda: books._bounded_book_digest(artifact, budget()), OSError)
        for code in sorted(books._UNSUPPORTED_BOOK_IO_ERRORS):
            with self.subTest(errno=code), patch.object(
                    books, '_book_target_version', side_effect=OSError(code, 'fixture')), \
                    self.assertRaises(OSError) as caught:
                books._bounded_book_digest(artifact, budget())
            self.assertEqual(code, caught.exception.errno)

    def test_original_checks_report_io_errors_as_refusals(self):
        artifact = self.root / 'state.json'
        artifact.write_bytes(b'{}')
        info = artifact.lstat()
        row = {'old_sha256': 'x', 'old_file_identity': [info.st_dev, info.st_ino, info.st_size,
                                                         info.st_mtime_ns, info.st_mode, info.st_nlink]}
        budget = books._BookScanBudget(1024, 8, None)
        with patch.object(books, '_book_target_version', side_effect=OSError(errno.EIO, 'fixture')):
            self.assert_refused(lambda: books._validate_byte_original(row, artifact), OSError)
            self.assert_refused(lambda: books._validate_stream_original(row, artifact, budget), OSError)


class BookIoErrorCoverageTests(unittest.TestCase):
    """One check per decorated family, so dropping any decorator fails a test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'artifact'
        self.path.write_bytes(b'bytes')
        info = self.path.lstat()
        identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink]
        self.row = {'path': 'artifact', 'existed': True, 'old_sha256': '0' * 64, 'new_sha256': '0' * 64,
                    'old_file_identity': identity, 'new_file_identity': identity}
        self.version = books._book_info_version(info)
        # A stream row journals its replacement identity as [dev, ino].
        self.stream_row = {**self.row, 'new_file_identity': identity[:2]}

    def refused(self, call):
        failing = OSError(errno.EIO, 'fixture')
        with patch.object(books, '_book_target_version', side_effect=failing), \
                self.assertRaises(ValueError) as caught:
            call()
        self.assertIs(failing, caught.exception.__cause__, caught.exception)

    def test_every_decorated_family_reports_io_errors_as_refusals(self):
        budget = lambda: books._BookScanBudget(1024, 8, None)
        workspace = self.root / '.book-switch-fixture'
        workspace.mkdir()
        (workspace / 'old-0').write_bytes(b'bytes')
        stream_data = {'phase': 'committed', 'entries': [self.row],
                       'stream_limits': {'bytes': 1024, 'entries': 8}}
        calls = {
            'stream target': lambda: books._validate_stream_target(self.row, self.path, self.version, budget()),
            'moved stream original': lambda: books._validate_moved_stream_original(
                self.row, self.path, self.version, budget()),
            'moved byte original': lambda: books._validate_moved_byte_original(self.row, self.path, self.version),
            'moved byte replacement': lambda: books._validate_moved_byte_replacement(self.row, self.path),
            'byte replacement': lambda: books._validate_byte_replacement(self.row, self.path),
            'stream replacement': lambda: books._validate_stream_replacement(
                self.stream_row, self.path, budget()),
            'committed targets': lambda: books._verify_committed_stream_targets(
                [self.stream_row], [self.path], budget()),
            'stream workspace': lambda: books._validate_stream_workspace_for_cleanup(workspace, stream_data),
            'byte workspace': lambda: books._validate_byte_workspace_for_cleanup(
                workspace, {'phase': 'committed', 'entries': [self.row]}),
        }
        for label, call in calls.items():
            with self.subTest(label):
                self.refused(call)

    def test_staging_reads_are_refusals(self):
        (self.root / 'state.json').write_bytes(b'old')
        failing = PermissionError(errno.EACCES, 'fixture')
        version = books._book_target_version
        def unreadable_target(path):
            if Path(path).name == 'state.json':
                raise failing
            return version(path)
        with patch.object(books, '_book_target_version', side_effect=unreadable_target), \
                self.assertRaisesRegex(ValueError, 'could not be read during staging') as caught:
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])
        self.assertIs(failing, caught.exception.__cause__)
        self.assertEqual(b'old', (self.root / 'state.json').read_bytes())
        self.assertFalse((self.root / books.JOURNAL).exists())

    def test_an_unreadable_workspace_is_not_treated_as_absent(self):
        # os.path.lexists reported EACCES as "absent", so cleanup retired the
        # journal while the workspace remained.
        workspace = self.root / '.book-switch-fixture'
        self.assertFalse(books._is_book_workspace_present(workspace))
        self.assertFalse(books._is_book_workspace_present(self.path / 'below-a-file'))
        workspace.mkdir()
        self.assertTrue(books._is_book_workspace_present(workspace))
        lstat = os.lstat
        def denied(path, *args, **kwargs):
            if Path(path) == workspace:
                raise PermissionError(errno.EACCES, 'fixture')
            return lstat(path, *args, **kwargs)
        with patch.object(books.os, 'lstat', side_effect=denied):
            with self.assertRaisesRegex(ValueError, 'workspace could not be read'):
                books._is_book_workspace_present(workspace)
            self.assertTrue(os.path.lexists(workspace) is False)   # what lexists used to report
        for code in sorted(books._UNSUPPORTED_BOOK_IO_ERRORS):
            with self.subTest(errno=code), patch.object(
                    books.os, 'lstat', side_effect=OSError(code, 'fixture')), \
                    self.assertRaises(OSError) as caught:
                books._is_book_workspace_present(workspace)
            self.assertEqual(code, caught.exception.errno)


if __name__ == '__main__':
    unittest.main()
