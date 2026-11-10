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

    def test_finish_keeps_a_writer_replacement_and_retires_the_journal(self):
        # v4 aborts a transaction whose target a writer replaced before the
        # swap: the writer's object stays, our staged replacement is deleted,
        # and the journal does not stay fenced forever (#1660). Windows and
        # an unsupported filesystem keep the v3 refusal.
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        code, result = self.run_command('finish')
        if os.name == 'nt':
            self.assertEqual(command.EXIT_REFUSED, code)
            self.assertTrue((self.root / books.JOURNAL).exists())
            return
        self.assertEqual(command.EXIT_OK, code)
        self.assertEqual(b'someone else', self.target.read_bytes())
        self.assertFalse((self.root / books.JOURNAL).exists())
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

    def test_an_operator_quarantine_interrupted_before_any_move_is_rerun(self):
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        with patch.object(books, '_move', side_effect=_Crash()), self.assertRaises(_Crash):
            self.run_command('quarantine', '--accept-current-files')
        code, result = self.run_command('inspect')
        [entry] = result['report']['quarantines']
        self.assertEqual(('prepared', True, False), (entry['state'], entry['forced'], entry['journal_held']))
        # Nothing moved yet: the saved flag alone does not force recovery.
        self.assertEqual(command.EXIT_REFUSED, self.run_command('finish')[0])
        self.assertEqual(0, self.run_command('quarantine', '--accept-current-files')[0])
        self.assertEqual(1, len(self.quarantines()))   # the same quarantine, resumed
        self.assertFalse((self.root / books.JOURNAL).exists())

    def test_an_operator_quarantine_interrupted_after_the_workspace_move_is_finished(self):
        self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        move = books._move
        def die_on_journal(source, destination, **kwargs):
            if Path(source).name == books.JOURNAL:
                raise _Crash()
            return move(source, destination, **kwargs)
        with patch.object(books, '_move', side_effect=die_on_journal), self.assertRaises(_Crash):
            self.run_command('quarantine', '--accept-current-files')
        # Past the point of no return, recovery keeps the operator's decision.
        self.assertEqual(0, self.run_command('finish')[0])
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual(b'someone else', self.target.read_bytes())

    def test_a_forced_receipt_written_by_hand_does_not_force_recovery(self):
        workspace = self.interrupted_switch()
        self.target.write_bytes(b'someone else')
        raw = (self.root / books.JOURNAL).read_bytes()
        import hashlib
        sha = hashlib.sha256(raw).hexdigest()
        for forced in (True, 1, 'yes'):
            with self.subTest(forced=forced):
                forged = self.root / f'{books.QUARANTINE_PREFIX}{sha[:8]}-deadbeef'
                forged.mkdir()
                (forged / books.QUARANTINE_RECEIPT).write_text(json.dumps(
                    {'version': 1, 'state': 'prepared', 'forced': forced, 'refusal': 'x',
                     'journal': {'sha256': sha, 'bytes': len(raw), 'version': 3, 'phase': 'publishing'},
                     'workspace': {'name': workspace.name,
                                   'identity': [workspace.lstat().st_dev, workspace.lstat().st_ino]}}))
                with self.assertRaises(ValueError):
                    with books.ensure_book_state(self.root):
                        pass
                self.assertEqual(raw, (self.root / books.JOURNAL).read_bytes())
                self.assertTrue(workspace.exists())
                (forged / books.QUARANTINE_RECEIPT).unlink()
                forged.rmdir()

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
        self.assertTrue(workspace.is_dir())

    def test_a_malformed_journal_is_refused_not_moved(self):
        (self.root / books.JOURNAL).write_bytes(b'not json')
        for argv in (('inspect',), ('finish',), ('quarantine', '--accept-current-files')):
            with self.subTest(argv=argv):
                self.assertEqual(command.EXIT_REFUSED, self.run_command(*argv)[0])
        self.assertEqual(b'not json', (self.root / books.JOURNAL).read_bytes())
        self.assertEqual([], self.quarantines())

    def test_a_malformed_v1_journal_is_refused_not_sent_to_migration(self):
        (self.root / books.JOURNAL).write_text(json.dumps(
            {'version': 1, 'phase': 'committed', 'workspace': '.book-switch-' + 'a' * 32,
             'entries': [{'path': 'state.json', 'existed': 'maybe'}]}))
        for argv in (('inspect',), ('finish',)):
            with self.subTest(argv=argv):
                code = self.run_command(*argv)[0]
                self.assertNotEqual(command.EXIT_MIGRATION, code)
        self.assertEqual(command.EXIT_REFUSED, self.run_command('finish')[0])

    def test_hash_says_which_bytes_each_file_holds(self):
        self.interrupted_switch()
        self.assertEqual('old', self.run_command('inspect', '--hash')[1]
                         ['report']['entries'][0]['current']['holds'])
        # Same size, so identity and a coarse timestamp may not show it.
        self.target.write_bytes(b'odd')
        [entry] = self.run_command('inspect', '--hash')[1]['report']['entries']
        self.assertEqual('neither', entry['current']['holds'])
        self.assertNotIn('sha256', self.run_command('inspect')[1]['report']['entries'][0]['current'])

    def test_inspect_does_not_follow_a_link_named_like_a_quarantine(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / books.QUARANTINE_RECEIPT).write_text(json.dumps(
            {'version': 1, 'state': 'completed', 'forced': True, 'refusal': 'outside'}))
        link = self.root / f'{books.QUARANTINE_PREFIX}0000000b-55555555'
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), outside.name],
                           check=True, capture_output=True)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            link.symlink_to(outside.name, target_is_directory=True)
        [entry] = self.run_command('inspect')[1]['report']['quarantines']
        self.assertEqual('Not a plain directory; not read', entry['error'])
        self.assertNotIn('refusal', entry)

    def test_inspect_reports_a_hostile_receipt_with_plain_types(self):
        hostile = self.root / f'{books.QUARANTINE_PREFIX}0000000c-66666666'
        hostile.mkdir()
        (hostile / books.QUARANTINE_RECEIPT).write_text(json.dumps(
            {'state': {'nested': 1}, 'forced': 'yes', 'journal': {'bytes': 5}}))
        (hostile / books.QUARANTINE_JOURNAL).write_bytes(b'0123456789')
        [entry] = self.run_command('inspect')[1]['report']['quarantines']
        self.assertEqual((None, False, False), (entry['state'], entry['forced'], entry['journal_held']))
        (hostile / books.QUARANTINE_RECEIPT).write_text('[]')
        [entry] = self.run_command('inspect')[1]['report']['quarantines']
        self.assertEqual('Receipt is not an object', entry['error'])

    def test_operator_quarantine_never_adopts_a_directory_that_is_not_the_workspace(self):
        workspace = self.interrupted_switch()
        os.rename(workspace, self.root / 'elsewhere')
        workspace.mkdir()
        (workspace / 'not ours').write_bytes(b'user data')
        self.assertEqual(command.EXIT_REFUSED, self.run_command('quarantine', '--accept-current-files')[0])
        self.assertEqual(b'user data', (workspace / 'not ours').read_bytes())
        self.assertEqual([], self.quarantines())

    def test_wait_takes_the_lock_once_it_is_released(self):
        self.interrupted_switch()
        holding = threading.Event()
        def hold():
            with books.book_operator_locked(self.root):
                holding.set()
                threading.Event().wait(0.5)
        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        holding.wait(10)
        self.assertEqual(0, self.run_wait('10'))

    def run_wait(self, seconds):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return command.main(['--data-dir', str(self.root), '--json', '--wait', seconds, 'inspect'])

    def test_bad_waits_and_a_relative_data_dir_are_usage_or_refusal_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ('nan', 'inf', '-1'):
                with self.subTest(wait=value):
                    self.assertEqual(command.EXIT_USAGE, command.main(
                        ['--data-dir', str(self.root), '--wait', value, 'inspect']))
        with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': 'relative-data'}):
            self.assertEqual(command.EXIT_REFUSED, self.run_command_without_dir('inspect'))

    def run_command_without_dir(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return command.main(['--json', *argv])

    def test_an_uncertain_journal_write_is_a_system_error_with_the_report(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        uncertain = books.BookJournalPublicationUncertain('uncertain write')
        with patch.object(books, 'recover_book_state_locked', side_effect=uncertain):
            code, result = self.run_command('finish')
        self.assertEqual(command.EXIT_SYSTEM, code)
        self.assertEqual('publishing', result['report']['journal']['phase'])

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
        import shutil
        workspace = self.interrupted_switch()
        # A non-directory at the journal's workspace is a refusal, so this
        # exercises a non-zero exit propagated by the process.
        shutil.rmtree(workspace)
        workspace.write_bytes(b'not a directory')
        completed = subprocess.run(
            [sys.executable, str(APP_DIR / 'book_state_recovery.py'), '--data-dir', str(self.root),
             '--json', 'finish'],
            cwd=APP_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(command.EXIT_REFUSED, completed.returncode, completed.stderr)
        self.assertEqual('finish', json.loads(completed.stdout)['command'])


if __name__ == '__main__':
    unittest.main()
