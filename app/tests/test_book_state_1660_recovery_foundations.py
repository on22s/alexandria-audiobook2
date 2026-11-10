"""#1660 recovery groundwork: the shared book-operation lock, the exact
journal bytes, the read-only inspect report and the reserved evidence names."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books


class _Crash(BaseException):
    """Process death: the transaction's own cleanup must not run."""


class RecoveryFoundationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def interrupted_switch(self):
        (self.root / 'state.json').write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.is_dir(), path.lstat().st_mtime_ns,
                                                    None if path.is_dir() else path.read_bytes())
                for path in sorted(self.root.rglob('*'))}

    def test_operator_lock_holds_the_lock_without_admitting_the_book(self):
        with books.book_operator_locked(self.root) as (root, identity):
            self.assertEqual(books._direct_book_root(self.root), (root, identity))
            self.assertFalse(books.is_book_state_admitted(self.root))
            with self.assertRaises(TimeoutError):
                with books.book_operator_locked(self.root, timeout=0.2):
                    pass
        with books.book_operator_locked(self.root, timeout=0.2):
            pass   # released on exit

    def test_inspect_reports_nothing_pending_for_a_clean_book(self):
        before = self.snapshot()
        root_before = self.root.stat().st_mtime_ns
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual((before, root_before), (self.snapshot(), self.root.stat().st_mtime_ns))
        self.assertEqual({'present': False}, report['journal'])
        self.assertEqual(([], [], 'complete'),
                         (report['entries'], report['retained'], report['retained_status']))

    def write_journal(self, **fields):
        data = {'version': 3, 'phase': 'committed', 'workspace': '.book-switch-' + 'a' * 32,
                'entries': [{'path': 'state.json', 'existed': False,
                             'old_sha256': None, 'new_sha256': None}]}
        data.update(fields)
        (self.root / books.JOURNAL).write_text(json.dumps(data), encoding='utf-8')

    def test_inspect_reports_hostile_shapes_instead_of_raising(self):
        for fields in ({'workspace': '.book-switch-' + chr(0) + 'x'}, {'entries': 'nope'},
                       {'entries': [1, {'path': '../escape'}, {'path': 'a' + chr(0) + 'b'}, {'path': 7}]}):
            with self.subTest(fields=fields):
                self.write_journal(**fields)
                report = books.inspect_book_recovery_state(self.root)
                self.assertTrue(report['journal']['present'])

    def test_inspect_refuses_more_rows_than_recovery_accepts(self):
        from book_recovery_archive import MAX_FILES
        self.write_journal(entries=[{'path': f'f{i}'} for i in range(MAX_FILES + 1)])
        report = books.inspect_book_recovery_state(self.root)
        self.assertIn('recoverable', report['journal']['error'])
        self.assertEqual([], report['entries'])

    def test_inspect_rejects_an_invalid_entry_limit(self):
        for limit in (None, 'x', 0, -1, 2.5, True, books.MAX_LEGACY_CLEANUP_ENTRIES + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                books.inspect_book_recovery_state(self.root, entry_limit=limit)

    def test_inspect_says_when_the_retained_listing_is_truncated(self):
        for index in range(3):
            (self.root / f'{books.MIGRATION_PREFIX}m{index}').mkdir()
        report = books.inspect_book_recovery_state(self.root, entry_limit=2)
        self.assertEqual('truncated', report['retained_status'])

    def test_inspect_reports_an_interrupted_switch_without_changing_anything(self):
        self.interrupted_switch()
        journal = self.root / books.JOURNAL
        raw = journal.read_bytes()
        before = self.snapshot()
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual(before, self.snapshot())   # read-only
        summary = report['journal']
        self.assertEqual(('publishing', 3, len(raw), hashlib.sha256(raw).hexdigest()),
                         (summary['phase'], summary['version'], summary['bytes'], summary['sha256']))
        self.assertEqual('directory', summary['workspace_state']['kind'])
        self.assertEqual(['new-0'], summary['workspace_listing']['names'])
        [entry] = report['entries']
        self.assertEqual(('state.json', True, 'file'),
                         (entry['path'], entry['expected']['existed'], entry['current']['kind']))
        data, _version, exact = books._read_book_journal(journal, include_raw=True)
        self.assertEqual(raw, exact)
        self.assertEqual(json.loads(raw), data)

    def test_inspect_reports_a_malformed_journal_instead_of_raising(self):
        (self.root / books.JOURNAL).write_bytes(b'not json')
        report = books.inspect_book_recovery_state(self.root)
        self.assertTrue(report['journal']['present'])
        self.assertIn('Invalid active-book transaction', report['journal']['error'])

    def test_a_journal_never_points_recovery_at_retained_evidence(self):
        # Case variants too: Windows and macOS open the same directory.
        for prefix in (books.QUARANTINE_PREFIX, books.MIGRATION_PREFIX,
                       '.book-switch-Quarantine-', '.BOOK-SWITCH-MIGRATION-'):
            with self.subTest(prefix=prefix):
                evidence = self.root / (prefix + 'deadbeef-00112233')
                evidence.mkdir()
                (self.root / books.JOURNAL).write_text(json.dumps(
                    {'version': 3, 'phase': 'committed', 'workspace': evidence.name,
                     'entries': [{'path': 'state.json', 'existed': False,
                                  'old_sha256': None, 'new_sha256': None}]}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Invalid active-book workspace'):
                    with books.ensure_book_state(self.root):
                        pass
                self.assertTrue(evidence.exists())
                self.assertIn(evidence.name, books.inspect_book_recovery_state(self.root)['retained'])
                (self.root / books.JOURNAL).unlink()
                evidence.rmdir()


if __name__ == '__main__':
    unittest.main()
