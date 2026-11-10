"""#1660 step 5 (L1): report a crashed switch's leftovers, never reclaim them.

A crash before its journal is published leaves `.book-switch-<id>/` staging
that no record mentions; a failed journal publication leaves
`.active_book_transaction.json.new-<id>`. `inspect` reports both read-only and
the summary reports counts only, so nothing foreign is ever deleted or moved.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import book_state_recovery as command
import book_state_transaction as books


def staging_name(hex_text='a' * 32):
    return '.book-switch-' + hex_text


def temporary_name(hex_text='b' * 32):
    return books.JOURNAL + '.new-' + hex_text


def make_link(target, link):
    """A directory link without requiring symlink privilege on Windows."""
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def tree(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(Path(directory).rglob('*')) if path.is_file()}


class LeftoverReportTests(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def names(self, report, key):
        return [entry['name'] for entry in report[key]]

    def test_orphan_staging_is_reported_with_its_bytes(self):
        orphan = self.root / staging_name()
        orphan.mkdir()
        (orphan / 'new-0').write_bytes(b'x' * (1024 * 1024))
        # Depth 3 is not walked: a nested tree cannot make this scan unbounded.
        (orphan / 'sub' / 'deep').mkdir(parents=True)
        (orphan / 'sub' / 'deep' / 'ignored.bin').write_bytes(b'ignored')
        before = tree(orphan)
        report = books.inspect_book_recovery_state(self.root)
        [entry] = report['orphaned_staging']
        self.assertEqual(orphan.name, entry['name'])
        self.assertEqual('directory', entry['kind'])
        self.assertEqual((1024 * 1024, 3, False),
                         (entry['bytes'], entry['entries'], entry['truncated']))
        self.assertIsInstance(entry['newest_mtime_ns'], int)
        for _attempt in range(2):
            with books.ensure_book_state(self.root):
                pass
            self.assertEqual(before, tree(orphan))
        self.assertEqual([entry['name']],
                         self.names(books.inspect_book_recovery_state(self.root),
                                    'orphaned_staging'))

    def test_the_native_workspace_shape_is_reported_where_the_app_uses_it(self):
        # POSIX names a workspace with uuid4().hex; Windows uses
        # tempfile.mkdtemp (8 lowercase alphanumerics). A name that only
        # resembles one still is not reported.
        orphan = self.root / staging_name('s3_9a74x')
        orphan.mkdir()
        names = self.names(books.inspect_book_recovery_state(self.root), 'orphaned_staging')
        self.assertEqual([orphan.name] if os.name == 'nt' else [], names)

    def test_the_journals_own_workspace_is_never_an_orphan(self):
        workspace = self.root / staging_name('c' * 32)
        workspace.mkdir()
        (workspace / 'new-0').write_bytes(b'live')
        orphan = self.root / staging_name('d' * 32)
        orphan.mkdir()
        self.assertIsNotNone(books._STAGING_LEFTOVER_NAME.fullmatch(workspace.name))
        (self.root / books.JOURNAL).write_text(json.dumps({
            'version': 2, 'phase': 'publishing', 'workspace': workspace.name,
            'entries': []}), encoding='utf-8')
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual([orphan.name], self.names(report, 'orphaned_staging'))

    def test_an_unreadable_journal_reports_no_orphans(self):
        orphan = self.root / staging_name()
        orphan.mkdir()
        self.assertIsNotNone(books._STAGING_LEFTOVER_NAME.fullmatch(orphan.name))
        (self.root / books.JOURNAL).write_bytes(b'{not json')
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual([], report['orphaned_staging'])
        self.assertIn('error', report['journal'])

    def test_resembling_names_are_not_orphans(self):
        real_staging = self.root / staging_name('e' * 32)
        real_staging.mkdir()
        real_temporary = self.root / temporary_name('f' * 32)
        real_temporary.write_bytes(b'{}')
        for name in ('.book-switch-' + 'a' * 31, '.book-switch-' + 'a' * 33,
                     '.book-switch-' + 'A' * 32, '.book-switch-' + 'g' * 32,
                     'x.book-switch-' + 'a' * 32,
                     books.JOURNAL + '.new-' + 'a' * 31,
                     books.JOURNAL + '.new-' + 'A' * 32):
            (self.root / name).mkdir() if not name.startswith(books.JOURNAL) else \
                (self.root / name).write_bytes(b'{}')
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual([real_staging.name], self.names(report, 'orphaned_staging'))
        self.assertEqual([real_temporary.name], self.names(report, 'journal_temporaries'))

    def test_a_link_named_like_staging_is_not_reported(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'new-0').write_bytes(b'not ours')
        link = self.root / staging_name()
        make_link(target, link)
        self.assertIsNotNone(books._STAGING_LEFTOVER_NAME.fullmatch(link.name))
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual([], report['orphaned_staging'])
        self.assertTrue(link.exists())
        self.assertEqual(b'not ours', (target / 'new-0').read_bytes())

    def test_a_plain_file_named_like_staging_is_reported_without_a_scan(self):
        name = staging_name()
        (self.root / name).write_bytes(b'12345')
        report = books.inspect_book_recovery_state(self.root)
        [entry] = report['orphaned_staging']
        self.assertEqual((name, 'other', 5, 0),
                         (entry['name'], entry['kind'], entry['bytes'], entry['entries']))

    def test_a_journal_temporary_is_reported_with_its_bytes(self):
        name = temporary_name()
        (self.root / name).write_bytes(b'partial journal')
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual([{'name': name, 'bytes': 15}], report['journal_temporaries'])

    def test_inspect_is_bounded_by_the_entry_limit(self):
        orphan = self.root / staging_name()
        orphan.mkdir()
        for index in range(5):
            (orphan / f'new-{index}').write_bytes(b'x')
        [entry] = books.inspect_book_recovery_state(
            self.root, entry_limit=3)['orphaned_staging']
        self.assertEqual((3, True), (entry['entries'], entry['truncated']))

    def test_summary_counts_leftovers_and_keeps_status_clean(self):
        orphan = self.root / staging_name()
        orphan.mkdir()
        (orphan / 'new-0').write_bytes(b'x' * 100)
        (self.root / temporary_name()).write_bytes(b'y' * 7)
        summary = command.get_book_recovery_summary(self.root)
        self.assertEqual('clean', summary['status'])
        self.assertFalse(summary['locked'])
        self.assertEqual({'orphaned_staging': 1, 'journal_temporaries': 1, 'bytes': 107},
                         summary['leftovers'])
        rendered = json.dumps(summary)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(orphan.name, rendered)

    def test_summary_omits_leftovers_while_locked(self):
        orphan = self.root / staging_name()
        orphan.mkdir()
        (orphan / 'new-0').write_bytes(b'x')
        holding, release = threading.Event(), threading.Event()
        def hold():
            with books.book_operator_locked(self.root):
                holding.set()
                release.wait(10)
        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(holding.wait(10))
        summary = command.get_book_recovery_summary(self.root, wait=0.05)
        self.assertTrue(summary['locked'])
        self.assertIsNone(summary['leftovers'])


if __name__ == '__main__':
    unittest.main()
