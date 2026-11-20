"""Issue #2106: the root_fd=None journal-update fallback must re-check the
observed version immediately before publication, like the POSIX branch does.

These tests drive the fallback explicitly (root_fd is left at its None default)
so they exercise the Windows code path on every platform, including Linux CI.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books


REFUSED = 'Book transaction journal changed; update was refused'


class BookJournalFallbackRecheckTests(unittest.TestCase):
    def test_fallback_refuses_journal_replaced_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = books._save_journal(root, {'phase': 'publishing'}, create=True)
            journal = root / books.JOURNAL
            foreign = b'{"foreign":"journal"}'
            observed = []
            real_version = books._book_target_version
            def replace_on_second_observation(path):
                # The first observation is the entry check (~line 1410). The
                # second is either the new pre-publication re-check (fixed) or
                # the post-write verify (unfixed). Land the external writer
                # there so it lands inside the update window.
                if len(observed) == 1:
                    journal.write_bytes(foreign)
                result = real_version(path)
                observed.append(result)
                return result
            with patch.object(books, '_book_target_version',
                              side_effect=replace_on_second_observation):
                with self.assertRaises(ValueError) as caught:
                    books._save_journal(root, {'phase': 'committed'},
                                        expected_version=version)
            self.assertEqual(REFUSED, str(caught.exception))
            self.assertEqual(2, len(observed))
            self.assertEqual(foreign, journal.read_bytes())

    def test_fallback_normal_update_publishes_its_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            version = books._save_journal(root, {'phase': 'publishing'}, create=True)
            updated = books._save_journal(root, {'phase': 'committed'},
                                          expected_version=version)
            self.assertEqual({'phase': 'committed'}, json.loads(journal.read_bytes()))
            self.assertEqual(books._book_target_version(journal), updated)

    def test_fallback_new_journal_without_expected_version_still_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            self.assertFalse(journal.exists())
            result = books._save_journal(root, {'phase': 'publishing'})
            self.assertEqual({'phase': 'publishing'}, json.loads(journal.read_bytes()))
            self.assertEqual(books._book_target_version(journal), result)


if __name__ == '__main__':
    unittest.main()
