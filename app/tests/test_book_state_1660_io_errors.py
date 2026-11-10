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


if __name__ == '__main__':
    unittest.main()
