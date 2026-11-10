"""#1660 step 5 (L2): finish a quarantine receipt a crash left prepared.

A death between the quarantine's moves and its completed write left a receipt
nothing revisited, because the journal is already gone. The next book operation
re-proves the receipt's journal and workspace and only then writes completed;
an unprovable receipt stays prepared and nothing moves.
"""
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books


class _Crash(BaseException):
    """Process death: nothing in the transaction may catch or clean up after it."""


def stream(data):
    return books.BookStateStream(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


class QuarantineCompletionTests(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.target = self.root / 'state.json'

    def root_identity(self):
        return books._direct_book_root(self.root)

    def quarantines(self):
        return sorted(entry for entry in self.root.iterdir()
                      if books._QUARANTINE_NAME.fullmatch(entry.name))

    def receipt(self, directory):
        return json.loads((directory / books.QUARANTINE_RECEIPT).read_text(encoding='utf-8'))

    def snapshot(self, directory):
        return {path.relative_to(directory).as_posix():
                (None if path.is_dir() else path.read_bytes())
                for path in sorted(directory.rglob('*'))}

    def fixture(self, *, journal_bytes=b'{"fake": 1}', workspace=True,
                state='prepared', journal_sha256=None, journal_size=None,
                name_hex='0' * 8 + '-' + '1' * 8):
        """A hand-built quarantine folder with a valid prepared receipt."""
        directory = self.root / (books.QUARANTINE_PREFIX + name_hex)
        directory.mkdir()
        held = directory / books.QUARANTINE_JOURNAL
        held.write_bytes(journal_bytes)
        identity = None
        if workspace:
            moved = directory / books.QUARANTINE_WORKSPACE
            moved.mkdir()
            (moved / 'new-0').write_bytes(b'staged')
            info = os.lstat(moved)
            identity = [info.st_dev, info.st_ino]
        receipt = {'version': 1, 'state': state, 'refusal': 'test',
                   'forced': False,
                   'journal': {'sha256': journal_sha256 or hashlib.sha256(journal_bytes).hexdigest(),
                               'bytes': len(journal_bytes) if journal_size is None else journal_size,
                               'version': 2, 'phase': 'publishing'},
                   'workspace': {'name': '.book-switch-' + 'a' * 32, 'identity': identity}}
        (directory / books.QUARANTINE_RECEIPT).write_text(
            json.dumps(receipt), encoding='utf-8')
        return directory

    def interrupted_switch(self):
        """A switch that fails after publishing its journal, before any move."""
        self.target.write_bytes(b'old')
        fired = []
        def crash(*args, **kwargs):
            fired.append(1)
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])
        # The failure hook must have run; a patched path that never fires
        # would make this fixture (and every test built on it) vacuous.
        self.assertGreaterEqual(len(fired), 2)
        journal = json.loads((self.root / books.JOURNAL).read_text(encoding='utf-8'))
        return self.root / journal['workspace']

    def prepared_quarantine(self):
        """S7: both moves done, then the completed write dies."""
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        write = books._write_quarantine_receipt
        fired = []
        def die_on_completion(directory, receipt):
            fired.append(receipt['state'])
            if receipt['state'] == 'completed':
                raise _Crash()
            return write(directory, receipt)
        with patch.object(books, '_write_quarantine_receipt', side_effect=die_on_completion), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        # The crash seam must have been reached, not skipped.
        self.assertIn('prepared', fired)
        self.assertEqual('completed', fired[-1])
        [quarantine] = self.quarantines()
        self.assertEqual('prepared', self.receipt(quarantine)['state'])
        self.assertFalse((self.root / books.JOURNAL).exists())
        return quarantine

    def test_completed_is_written_for_a_moved_journal(self):
        quarantine = self.prepared_quarantine()
        before = self.snapshot(quarantine)
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual('completed', self.receipt(quarantine)['state'])
        after = self.snapshot(quarantine)
        self.assertEqual(set(before) - {books.QUARANTINE_RECEIPT},
                         set(after) - {books.QUARANTINE_RECEIPT})
        for name in set(before) - {books.QUARANTINE_RECEIPT}:
            self.assertEqual(before[name], after[name])
        report = books.inspect_book_recovery_state(self.root)
        [entry] = report['quarantines']
        self.assertEqual(('completed', True, True),
                         (entry['state'], entry['journal_held'], entry['workspace_held']))

    def test_a_changed_quarantine_journal_stays_prepared(self):
        same_size = self.fixture(
            journal_bytes=b'{"fake": 1}',
            journal_sha256=hashlib.sha256(b'{"fake": 2}').hexdigest(),
            name_hex='0' * 8 + '-' + '2' * 8)
        truncated = self.fixture(journal_bytes=b'{"fake": 1}',
                                 name_hex='0' * 8 + '-' + '3' * 8, journal_size=4)
        valid = self.fixture(journal_bytes=b'{"fake": 2}', name_hex='0' * 8 + '-' + '4' * 8)
        before = {directory: self.snapshot(directory) for directory in (same_size, truncated)}
        with books.ensure_book_state(self.root):
            pass
        for directory in (same_size, truncated):
            self.assertEqual('prepared', self.receipt(directory)['state'])
            self.assertEqual(before[directory], self.snapshot(directory))
        self.assertEqual('completed', self.receipt(valid)['state'])
        by_name = {entry['name']: entry
                   for entry in books.inspect_book_recovery_state(self.root)['quarantines']}
        self.assertFalse(by_name[truncated.name]['journal_held'])

    def test_a_workspace_identity_mismatch_stays_prepared(self):
        real = self.prepared_quarantine()
        moved = real / books.QUARANTINE_WORKSPACE
        os.rename(moved, real / 'original')
        moved.mkdir()
        (moved / 'user data').write_bytes(b'not ours')
        valid = self.fixture(journal_bytes=b'{"fake": 2}', name_hex='0' * 8 + '-' + '5' * 8)
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual('prepared', self.receipt(real)['state'])
        self.assertEqual(b'not ours', (moved / 'user data').read_bytes())
        self.assertEqual('completed', self.receipt(valid)['state'])

    def test_a_receipt_with_no_moved_journal_is_left_to_recovery(self):
        quarantine = self.fixture()
        root, identity = self.root_identity()
        (self.root / books.JOURNAL).write_bytes(b'{"pending": true}')
        before = self.snapshot(quarantine)
        books._complete_interrupted_quarantines(root, identity)
        self.assertEqual('prepared', self.receipt(quarantine)['state'])
        self.assertEqual(before, self.snapshot(quarantine))
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_completed_and_empty_quarantine_dirs_are_left_alone(self):
        prepared = self.fixture(journal_bytes=b'{"fake": 1}',
                                name_hex='0' * 8 + '-' + '6' * 8)
        completed = self.fixture(journal_bytes=b'{"fake": 2}', state='completed',
                                 name_hex='0' * 8 + '-' + '7' * 8)
        empty = self.root / (books.QUARANTINE_PREFIX + '0' * 8 + '-' + '8' * 8)
        empty.mkdir()
        completed_before = self.snapshot(completed)
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual('completed', self.receipt(prepared)['state'])
        self.assertEqual(completed_before, self.snapshot(completed))
        self.assertEqual([], list(empty.iterdir()))

    def test_a_migration_folder_is_never_completed(self):
        directory = self.root / (books.MIGRATION_PREFIX + '0' * 8 + '-' + '9' * 8)
        directory.mkdir()
        journal_bytes = b'{"legacy": 1}'
        (directory / books.QUARANTINE_JOURNAL).write_bytes(journal_bytes)
        moved = directory / books.QUARANTINE_WORKSPACE
        moved.mkdir()
        info = os.lstat(moved)
        (directory / books.QUARANTINE_RECEIPT).write_text(json.dumps({
            'version': 1, 'state': 'prepared', 'refusal': 'legacy', 'forced': False,
            'kind': 'legacy-migration', 'trust': 'journal-digests',
            'journal': {'sha256': hashlib.sha256(journal_bytes).hexdigest(),
                        'bytes': len(journal_bytes), 'version': 1, 'phase': 'committed'},
            'workspace': {'name': '.book-switch-' + 'b' * 32,
                          'identity': [info.st_dev, info.st_ino]}}), encoding='utf-8')
        root, identity = self.root_identity()
        books._complete_interrupted_quarantines(root, identity)
        self.assertEqual('prepared', self.receipt(directory)['state'])

    def test_a_clean_root_writes_nothing(self):
        before = {path.name: (path.lstat().st_mtime_ns, path.lstat().st_ino)
                  for path in self.root.iterdir()}
        root, identity = self.root_identity()
        with patch.object(books.os, 'scandir', wraps=os.scandir) as scandir:
            books._complete_interrupted_quarantines(root, identity)
        self.assertEqual(1, scandir.call_count)
        after = {path.name: (path.lstat().st_mtime_ns, path.lstat().st_ino)
                 for path in self.root.iterdir()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
