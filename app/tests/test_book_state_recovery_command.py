"""#1660 operator recovery command: inspect / finish / quarantine."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_recovery as command
import book_state_transaction as books

APP_DIR = Path(__file__).parent.parent


class _Crash(BaseException):
    """Process death: the transaction's own cleanup must not run."""


class RecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.target = self.root / 'state.json'

    def run_command(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = command.main(['--data-dir', str(self.root), '--json', *argv])
        return code, json.loads(out.getvalue())

    def interrupted_switch(self):
        self.target.write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])
        return self.root / json.loads((self.root / books.JOURNAL).read_text())['workspace']

    def quarantines(self):
        return [entry for entry in self.root.iterdir()
                if entry.name.startswith(books.QUARANTINE_PREFIX)]

    def test_inspect_reports_a_clean_book(self):
        code, result = self.run_command('inspect')
        self.assertEqual((0, 'No book switch is pending.'), (code, result['message']))
        self.assertEqual([], result['report']['quarantines'])

    def test_inspect_reports_a_pending_switch_without_changing_it(self):
        workspace = self.interrupted_switch()
        code, result = self.run_command('inspect')
        self.assertEqual(0, code)
        self.assertEqual('publishing', result['report']['journal']['phase'])
        self.assertTrue(workspace.exists() and (self.root / books.JOURNAL).exists())

    def test_a_held_lock_is_busy_unless_inspecting_without_it(self):
        self.interrupted_switch()
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
        for argv in (('inspect',), ('finish',), ('quarantine', '--accept-current-files')):
            with self.subTest(argv=argv):
                self.assertEqual(command.EXIT_BUSY, self.run_command(*argv)[0])
        code, result = self.run_command('inspect', '--no-lock')
        self.assertEqual((0, 'publishing'), (code, result['report']['journal']['phase']))

    def test_finish_recovers_a_consistent_switch(self):
        self.interrupted_switch()
        code, result = self.run_command('finish')
        self.assertEqual((0, 'The pending switch was recovered.'), (code, result['message']))
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual(([], b'old'), (self.quarantines(), self.target.read_bytes()))

    def test_finish_reports_an_auto_quarantine(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        code, result = self.run_command('finish')
        [quarantine] = self.quarantines()
        self.assertEqual(0, code)
        self.assertIn(quarantine.name, result['message'])
        [entry] = result['report']['quarantines']
        self.assertEqual(('completed', False, True, True),
                         (entry['state'], entry['forced'], entry['journal_held'], entry['workspace_held']))

    def test_finish_refuses_when_a_book_file_changed(self):
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        code, result = self.run_command('finish')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertTrue(result['message'].startswith('Refused: '))
        self.assertTrue((self.root / books.JOURNAL).exists())
        self.assertEqual([], self.quarantines())

    def test_quarantine_requires_the_explicit_flag(self):
        self.interrupted_switch()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            command.main(['--data-dir', str(self.root), 'quarantine'])
        self.assertEqual(command.EXIT_USAGE, raised.exception.code)
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_quarantine_accepts_the_current_files_and_keeps_the_evidence(self):
        workspace = self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        code, result = self.run_command('quarantine', '--accept-current-files')
        self.assertEqual(0, code)
        [quarantine] = self.quarantines()
        receipt = json.loads((quarantine / books.QUARANTINE_RECEIPT).read_text())
        self.assertEqual(('completed', True), (receipt['state'], receipt['forced']))
        self.assertEqual(b'new', (quarantine / books.QUARANTINE_WORKSPACE / 'new-0').read_bytes())
        self.assertFalse(workspace.exists() or (self.root / books.JOURNAL).exists())
        self.assertEqual(b'someone else', self.target.read_bytes())
        with books.ensure_book_state(self.root):
            pass

    def test_an_interrupted_operator_quarantine_resumes_as_forced(self):
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        with patch.object(books, '_move', side_effect=_Crash()), self.assertRaises(_Crash):
            self.run_command('quarantine', '--accept-current-files')
        code, result = self.run_command('inspect')
        [entry] = result['report']['quarantines']
        self.assertEqual(('prepared', True, False), (entry['state'], entry['forced'], entry['journal_held']))
        # Plain recovery would refuse the changed target; the receipt keeps
        # the operator's decision.
        self.assertEqual(0, self.run_command('finish')[0])
        self.assertFalse((self.root / books.JOURNAL).exists())

    def test_a_legacy_journal_needs_migration(self):
        workspace = self.root / ('.book-switch-' + 'a' * 32)
        workspace.mkdir()
        raw = json.dumps({'version': 1, 'phase': 'committed', 'workspace': workspace.name,
                          'entries': [{'path': 'state.json', 'existed': False,
                                       'old_sha256': None, 'new_sha256': None}]}).encode()
        (self.root / books.JOURNAL).write_bytes(raw)
        for argv in (('inspect',), ('finish',), ('quarantine', '--accept-current-files')):
            with self.subTest(argv=argv):
                self.assertEqual(command.EXIT_MIGRATION, self.run_command(*argv)[0])
        self.assertEqual(raw, (self.root / books.JOURNAL).read_bytes())
        self.assertEqual([], self.quarantines())

    def test_a_malformed_journal_is_refused_not_moved(self):
        (self.root / books.JOURNAL).write_bytes(b'not json')
        code, result = self.run_command('quarantine', '--accept-current-files')
        self.assertEqual(command.EXIT_REFUSED, code)
        self.assertEqual(b'not json', (self.root / books.JOURNAL).read_bytes())
        self.assertEqual([], self.quarantines())

    def test_the_default_data_dir_is_the_apps(self):
        with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(self.root)}):
            from utils import get_runtime_data_dir
            self.assertEqual(get_runtime_data_dir(str(APP_DIR.parent)), command.get_default_data_dir())
            self.interrupted_switch()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, command.main(['--json', 'inspect']))
        self.assertEqual('publishing', json.loads(out.getvalue())['report']['journal']['phase'])

    def test_the_script_runs_and_exits_with_its_code(self):
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        completed = subprocess.run(
            [sys.executable, str(APP_DIR / 'book_state_recovery.py'), '--data-dir', str(self.root),
             '--json', 'finish'],
            cwd=APP_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(command.EXIT_REFUSED, completed.returncode, completed.stderr)
        self.assertEqual('finish', json.loads(completed.stdout)['command'])


if __name__ == '__main__':
    unittest.main()
