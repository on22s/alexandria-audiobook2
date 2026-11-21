"""The committed journal guard must run after its durable staged write.

Both publication paths (portable writer and POSIX directory descriptor) stage
the new journal to a temporary file and fsync it before the replace.  A source
edit in that final window must be refused while the destination still holds
byte-identical prior evidence; the guard may not sit before the staging write.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import book_state_transaction as books
import adapter_publication as publication


class BaseJournalFlushGuard(unittest.TestCase):
    EDIT = b'{"phase":"foreign edit at final flush"}'

    def _install_fsync_edit(self, journal):
        """Edit the destination on the first fsync: the staged write is done."""
        real_fsync = os.fsync
        calls = []

        def fsync_then_edit(fd):
            result = real_fsync(fd)
            calls.append(1)
            if len(calls) == 1:
                journal.write_bytes(self.EDIT)
            return result

        return patch.object(books.os, 'fsync', side_effect=fsync_then_edit), calls

    def _save(self, root, descriptor=None):
        return books._save_journal(
            root, {'stage': 'commit', 'value': 'new'},
            expected_version=books._book_target_version(root / books.JOURNAL),
            root_fd=descriptor)


class PortableJournalFlushGuardTests(BaseJournalFlushGuard):
    def test_final_flush_edit_is_refused_and_prior_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            journal.write_bytes(b'{"phase":"publishing","value":"old"}')
            hook, calls = self._install_fsync_edit(journal)
            with hook, self.assertRaises(ValueError):
                self._save(root)
            self.assertEqual(1, len(calls))
            self.assertEqual(self.EDIT, journal.read_bytes())
            self.assertNotIn(b'new', journal.read_bytes())
            self.assertEqual([], list(root.glob('.publication-*')))

    def test_guard_runs_after_staged_write_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            journal.write_bytes(b'{"phase":"publishing"}')
            observed = []

            def guard():
                observed.append(journal.read_bytes())

            publication.save_adapter_publication_bytes(
                str(journal), b'{"phase":"committed"}', before_replace=guard)
            self.assertEqual([b'{"phase":"publishing"}'], observed)
            self.assertEqual(b'{"phase":"committed"}', journal.read_bytes())

    def test_generic_writer_callers_do_not_need_a_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'report.json'
            publication.save_adapter_publication_bytes(str(target), b'{"ok":true}')
            self.assertEqual(b'{"ok":true}', target.read_bytes())


@unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd,
                     'POSIX directory descriptors required')
class DescriptorJournalFlushGuardTests(BaseJournalFlushGuard):
    def test_final_flush_edit_is_refused_and_prior_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            journal.write_bytes(b'{"phase":"publishing","value":"old"}')
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            hook, calls = self._install_fsync_edit(journal)
            try:
                with hook, self.assertRaises(ValueError):
                    self._save(root, descriptor=descriptor)
            finally:
                os.close(descriptor)
            self.assertEqual(1, len(calls))
            self.assertEqual(self.EDIT, journal.read_bytes())
            self.assertNotIn(b'new', journal.read_bytes())
            self.assertEqual(1, len(list(root.glob(books.JOURNAL + '.new-*'))))


if __name__ == '__main__':
    unittest.main()
