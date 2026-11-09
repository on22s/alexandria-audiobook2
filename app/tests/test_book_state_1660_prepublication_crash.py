"""#1660 (d): a crash between removing an unpublished workspace and retiring
its journal must leave state that recovery completes, not refuses forever."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
from tests.test_book_state_streaming import stream


class _Crash(BaseException):
    """Process death: nothing in the transaction may catch or clean up after it."""


class PrepublicationCrashTests(unittest.TestCase):
    def crash_at_journal_unlink(self):
        def crash(*args, **kwargs):
            raise _Crash()
        return patch.object(books, '_unlink_owned_book_journal', side_effect=crash)

    def assert_recovers(self, root, target, original):
        # The crash left the journal behind but the workspace already removed.
        self.assertTrue((root / books.JOURNAL).exists())
        self.assertEqual([], list(root.glob('.book-switch-*')))
        with books.ensure_book_state(root):   # startup / next admission recovers
            pass
        self.assertFalse((root / books.JOURNAL).exists())
        self.assertEqual(original, target.read_bytes())

    def test_stream_failure_before_first_move_survives_crash_before_journal_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            save, cancelled = books._save_journal, [False]
            def after_save(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing':
                    cancelled[0] = True
                return result
            with patch.object(books, '_save_journal', side_effect=after_save), \
                    self.crash_at_journal_unlink(), self.assertRaises(_Crash):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [],
                                                  stream_byte_limit=100, cancel_check=lambda: cancelled[0])
            self.assert_recovers(root, target, b'old audio')

    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd,
                         'POSIX directory descriptors required')
    def test_byte_failure_before_first_move_survives_crash_before_journal_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sub').mkdir()
            target = root / 'sub/file.json'
            target.write_bytes(b'original')
            with patch.object(books, '_require_transaction_parent_identities',
                              side_effect=ValueError('parent changed')), \
                    self.crash_at_journal_unlink(), self.assertRaises(_Crash):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, {'sub/file.json': b'replacement'}, [])
            self.assert_recovers(root, target, b'original')


if __name__ == '__main__':
    unittest.main()
