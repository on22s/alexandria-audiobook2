"""#1660 migrate-legacy for v1 journals, and the recovery summary the app shows."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_recovery as command
import book_state_transaction as books


class _Crash(BaseException):
    """Process death: the transaction's own cleanup must not run."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.workspace = self.root / ('.book-switch-' + 'a' * 32)

    def legacy(self, phase, rows, workspace_files=None):
        """Write a v1 journal for rows of (path, old bytes or None, new bytes or None)."""
        entries = [{'path': path, 'existed': old is not None,
                    'old_sha256': None if old is None else sha(old),
                    'new_sha256': None if new is None else sha(new)} for path, old, new in rows]
        if workspace_files is not None:
            self.workspace.mkdir()
            for name, data in workspace_files.items():
                (self.workspace / name).write_bytes(data)
        raw = json.dumps({'version': 1, 'phase': phase, 'workspace': self.workspace.name,
                          'entries': entries}).encode()
        (self.root / books.JOURNAL).write_bytes(raw)
        return raw

    def run_command(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = command.main(['--data-dir', str(self.root), '--json', *argv])
        return code, json.loads(out.getvalue())

    def migrations(self):
        return [entry for entry in self.root.iterdir() if entry.name.startswith(books.MIGRATION_PREFIX)]

    def test_a_committed_v1_switch_is_moved_aside_and_the_book_opens(self):
        (self.root / 'script.json').write_bytes(b'new script')
        raw = self.legacy('committed', [('script.json', b'old script', b'new script')],
                          {'old-0': b'old script'})
        code, result = self.run_command('migrate-legacy', '--trust-journal-hashes')
        self.assertEqual(0, code, result['message'])
        [migration] = self.migrations()
        self.assertIn(str(migration), result['message'])
        receipt = json.loads((migration / books.QUARANTINE_RECEIPT).read_text())
        self.assertEqual(('completed', 'legacy-migration', 'journal-digests'),
                         (receipt['state'], receipt['kind'], receipt['trust']))
        self.assertEqual(raw, (migration / books.QUARANTINE_JOURNAL).read_bytes())
        self.assertEqual(b'old script', (migration / books.QUARANTINE_WORKSPACE / 'old-0').read_bytes())
        self.assertFalse(self.workspace.exists() or (self.root / books.JOURNAL).exists())
        self.assertEqual(b'new script', (self.root / 'script.json').read_bytes())
        with books.ensure_book_state(self.root):
            pass
        [entry] = self.run_command('inspect')[1]['report']['quarantines']
        self.assertEqual(('legacy-migration', 'completed', True), (entry['kind'], entry['state'], entry['journal_held']))

    def test_publishing_and_recovered_v1_switches_need_the_originals(self):
        for phase in ('publishing', 'recovered'):
            with self.subTest(phase=phase):
                (self.root / 'script.json').write_bytes(b'old script')
                self.legacy(phase, [('script.json', b'old script', b'new script'),
                                    ('added.json', None, b'added')], {'new-0': b'new script'})
                self.assertEqual(0, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])
                self.assertEqual(b'old script', (self.root / 'script.json').read_bytes())
                self.assertFalse((self.root / books.JOURNAL).exists())
                for migration in self.migrations():
                    for path in sorted(migration.rglob('*'), reverse=True):
                        path.rmdir() if path.is_dir() else path.unlink()
                    migration.rmdir()

    def test_a_switch_interrupted_mid_publish_is_refused_with_each_row_named(self):
        (self.root / 'script.json').write_bytes(b'new script')   # published
        raw = self.legacy('publishing', [('script.json', b'old script', b'new script'),
                                         ('voices.json', b'old voices', b'new voices')],
                          {'old-0': b'old script', 'new-1': b'new voices'})
        (self.root / 'voices.json').write_bytes(b'old voices')   # not yet
        code, result = self.run_command('migrate-legacy', '--trust-journal-hashes')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertIn('script.json: does not hold the original bytes', result['message'])
        self.assertNotIn('voices.json', result['message'])
        self.assertIn('old-N', result['message'])
        self.assertEqual(raw, (self.root / books.JOURNAL).read_bytes())
        self.assertEqual([], self.migrations())
        self.assertEqual(b'old script', (self.workspace / 'old-0').read_bytes())

    def test_a_removed_file_that_is_back_is_a_mismatch(self):
        (self.root / 'gone.json').write_bytes(b'back again')
        self.legacy('committed', [('gone.json', b'gone', None)], {'old-0': b'gone'})
        code, result = self.run_command('migrate-legacy', '--trust-journal-hashes')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertIn('gone.json: should be absent', result['message'])

    def test_migrate_legacy_needs_its_flag_and_a_v1_journal(self):
        self.legacy('committed', [('script.json', b'old', b'new')])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            command.main(['--data-dir', str(self.root), 'migrate-legacy'])
        self.assertEqual(command.EXIT_USAGE, raised.exception.code)
        (self.root / books.JOURNAL).unlink()
        self.assertEqual(0, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])  # nothing pending
        self.legacy_v3_pending()
        code, result = self.run_command('migrate-legacy', '--trust-journal-hashes')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertIn('not a legacy v1 journal', result['message'])

    def legacy_v3_pending(self):
        (self.root / 'state.json').write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])

    def test_unsafe_v1_target_names_are_refused_not_migrated(self):
        self.legacy('committed', [('../outside.json', b'old', b'new')])
        self.assertEqual('invalid', books.get_book_journal_status(self.root))
        code, result = self.run_command('migrate-legacy', '--trust-journal-hashes')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertIn('not valid', result['message'])
        self.assertEqual(command.EXIT_REFUSED, self.run_command('inspect')[0])

    def test_a_directory_target_and_an_absent_workspace_migrate(self):
        chapters = self.root / 'chapters'
        (chapters / 'nested').mkdir(parents=True)
        (chapters / 'nested' / 'one').write_bytes(b'one')
        digest = books._get_digest(chapters)
        (self.root / books.JOURNAL).write_text(json.dumps(
            {'version': 1, 'phase': 'recovered', 'workspace': self.workspace.name,
             'entries': [{'path': 'chapters', 'existed': True, 'old_sha256': digest,
                          'new_sha256': None}]}))
        self.assertEqual(0, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])
        [migration] = self.migrations()
        self.assertFalse((migration / books.QUARANTINE_WORKSPACE).exists())
        self.assertEqual(b'one', (chapters / 'nested' / 'one').read_bytes())
        (chapters / 'nested' / 'one').write_bytes(b'two')   # one changed byte
        (self.root / books.JOURNAL).write_text(json.dumps(
            {'version': 1, 'phase': 'recovered', 'workspace': self.workspace.name,
             'entries': [{'path': 'chapters', 'existed': True, 'old_sha256': digest,
                          'new_sha256': None}]}))
        self.assertEqual(command.EXIT_REFUSED, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])

    def test_a_linked_workspace_is_never_migrated(self):
        (self.root / 'script.json').write_bytes(b'new script')
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / 'old-0').write_bytes(b'keep me')
        self.legacy('committed', [('script.json', b'old script', b'new script')])
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.workspace), outside.name],
                           check=True, capture_output=True)
            self.addCleanup(lambda: os.rmdir(self.workspace) if os.path.lexists(self.workspace) else None)
        else:
            self.workspace.symlink_to(outside.name, target_is_directory=True)
        self.assertEqual(command.EXIT_REFUSED, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])
        self.assertEqual([], self.migrations())
        self.assertEqual(b'keep me', (Path(outside.name) / 'old-0').read_bytes())

    def test_an_interrupted_migration_is_never_finished_automatically_and_resumes(self):
        (self.root / 'script.json').write_bytes(b'new script')
        raw = self.legacy('committed', [('script.json', b'old script', b'new script')],
                          {'old-0': b'old script'})
        move = books._move
        def die_on_journal(source, destination, **kwargs):
            if Path(source).name == books.JOURNAL:
                raise _Crash()
            return move(source, destination, **kwargs)
        with patch.object(books, '_move', side_effect=die_on_journal), self.assertRaises(_Crash):
            self.run_command('migrate-legacy', '--trust-journal-hashes')
        [migration] = self.migrations()
        # Ordinary recovery still refuses a v1 journal and touches nothing.
        with self.assertRaises(books.BookJournalMigrationRequired):
            with books.ensure_book_state(self.root):
                pass
        self.assertEqual(raw, (self.root / books.JOURNAL).read_bytes())
        self.assertEqual(0, self.run_command('migrate-legacy', '--trust-journal-hashes')[0])
        self.assertEqual([migration], self.migrations())
        self.assertEqual(raw, (migration / books.QUARANTINE_JOURNAL).read_bytes())


class RecoverySummaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def pending(self, *, foreign_target=False):
        (self.root / 'state.json').write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])
        if foreign_target:
            (self.root / 'state.json').write_bytes(b'someone else')

    def test_each_status_names_the_commands_that_help(self):
        self.assertEqual(('clean', []), self.status())
        self.pending(foreign_target=True)
        status, commands = self.status()
        self.assertEqual('pending', status)
        # finish before keeping the current files: it may still restore them.
        self.assertEqual(['inspect --hash', 'finish', 'quarantine --accept-current-files'],
                         [item['command'].split('book_state_recovery.py ')[1] for item in commands])
        (self.root / books.JOURNAL).write_text(json.dumps(
            {'version': 1, 'phase': 'committed', 'workspace': '.book-switch-' + 'b' * 32,
             'entries': [{'path': 'state.json', 'existed': False, 'old_sha256': None, 'new_sha256': None}]}))
        status, commands = self.status()
        self.assertEqual('migration_required', status)
        self.assertIn('migrate-legacy --trust-journal-hashes', commands[1]['command'])
        (self.root / books.JOURNAL).write_bytes(b'not json')
        self.assertEqual('unreadable', self.status()[0])
        (self.root / books.JOURNAL).write_text(json.dumps(   # parses, but invalid
            {'version': 1, 'phase': 'committed', 'workspace': '.book-switch-' + 'b' * 32,
             'entries': [{'path': '../x', 'existed': False, 'old_sha256': None, 'new_sha256': None}]}))
        self.assertEqual(('unreadable', ['inspect --hash']),
                         (self.status()[0], [item['command'].split('book_state_recovery.py ')[1]
                                             for item in self.status()[1]]))

    def test_the_first_offered_fix_for_a_recoverable_switch_restores_the_originals(self):
        (self.root / 'a.json').write_bytes(b'old-a')
        (self.root / 'b.json').write_bytes(b'old-b')
        validate = books._validate_staged_replacement
        def die_on_second(*args, **kwargs):
            published = self.root / 'a.json'
            if published.exists() and published.read_bytes() == b'new-a':
                raise _Crash()   # a.json already published, b.json not
            return validate(*args, **kwargs)
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=die_on_second),                 patch.object(books, '_retire_unpublished_journal', side_effect=crash),                 self.assertRaises(_Crash):
            with books.ensure_book_state(self.root), \
                    patch.object(books, 'recover_book_state_locked', side_effect=crash):
                # Process death: the failure handler's own rollback must not run.
                books.apply_book_state_locked(self.root, {'a.json': b'new-a', 'b.json': b'new-b'}, [])
        self.assertEqual(b'new-a', (self.root / 'a.json').read_bytes())   # half switched
        commands = command.get_book_recovery_summary(self.root)['commands']
        argv = commands[1]['command'].split('book_state_recovery.py ')[1].split()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, command.main(['--data-dir', str(self.root), *argv]))
        # Rolled back, not kept half switched.
        self.assertEqual((b'old-a', b'old-b'),
                         ((self.root / 'a.json').read_bytes(), (self.root / 'b.json').read_bytes()))
        self.assertFalse((self.root / books.JOURNAL).exists())

    def status(self):
        summary = command.get_book_recovery_summary(self.root)
        return summary['status'], summary['commands']

    def test_a_busy_lock_still_reports_without_waiting(self):
        self.pending(foreign_target=True)
        holding, release = threading.Event(), threading.Event()
        def hold():
            with books.book_operator_locked(self.root):
                holding.set()
                release.wait(10)
        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        holding.wait(10)
        summary = command.get_book_recovery_summary(self.root, wait=0.05)
        self.assertEqual(('pending', True), (summary['status'], summary['locked']))

    def test_the_route_reports_the_summary_read_only(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib
        self.pending(foreign_target=True)
        journal = (self.root / books.JOURNAL).read_bytes()
        app = FastAPI()
        app.include_router(lib.router)
        with patch.object(lib, 'DATA_DIR', str(self.root)):
            response = TestClient(app).get('/api/book/recovery')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(('pending', 'publishing'), (response.json()['status'], response.json()['phase']))
        self.assertNotIn(str(self.root), response.text)   # no local paths
        self.assertEqual(journal, (self.root / books.JOURNAL).read_bytes())
        with patch.object(lib, 'DATA_DIR', str(self.root / 'missing')):
            response = TestClient(app).get('/api/book/recovery')
        self.assertEqual(409, response.status_code)
        self.assertIn('inspect', response.json()['detail'])


if __name__ == '__main__':
    unittest.main()
