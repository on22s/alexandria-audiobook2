"""Protocol v4: replacement-only transactions use one verified exchange (#1660).

The forward path swaps each target with its staged replacement in one
RENAME_EXCHANGE/RENAME_SWAP, so a target is never absent. Recovery is driven
by identity, never by slot name: after publication the slot holds the
original. These tests enumerate every crash point between the journal write
and commit, then prove the next admission reaches a consistent book without
deleting anything that is not ours.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import book_state_transaction as books
import source_upload_publication as publication


class _Crash(BaseException):
    """A crash injected at a protocol seam; not an Exception on purpose."""


POSIX_ONLY = unittest.skipIf(os.name == 'nt', 'POSIX exchange publication')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot(root):
    """Book target bytes only; lock files, journals and retained evidence excluded."""
    result = {}
    for path in Path(root).rglob('*'):
        if not path.is_file() or path.name.endswith('.lock'):
            continue
        relative = path.relative_to(root)
        if any(part.startswith('.book-switch-') for part in relative.parts):
            continue
        if relative.name in (books.JOURNAL, books.JOURNAL + '.lock'):
            continue
        if relative.name.startswith(books.JOURNAL + '.new-'):
            continue
        result[relative.as_posix()] = path.read_bytes()
    return result


@POSIX_ONLY
class BookStateV4ReplacementTests(unittest.TestCase):

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _prepare(self, count=2):
        """-> the original bytes, written under stable, sorted target names."""
        values = {}
        for index in range(count):
            name = f'part-{index}.json'
            data = f'original-{index}'.encode()
            (self.root / name).write_bytes(data)
            values[name] = data
        return values

    def _replacement_values(self, count=2, streamed=False):
        values = {}
        for index in range(count):
            data = f'replacement-{index}'.encode()
            values[f'part-{index}.json'] = (
                books.BookStateStream(io.BytesIO(data), len(data), _sha(data))
                if streamed else data)
        return values

    def _apply(self, values, removals=(), *, streamed=False):
        options = {'stream_byte_limit': 1 << 20} if streamed else {}
        with books.ensure_book_state(str(self.root)):
            books.apply_book_state_locked(str(self.root), values, list(removals), **options)

    def _capture_journals(self):
        """Patch _save_journal to record each phase it durably writes."""
        records = []
        original = books._save_journal
        def record(root, data, **kwargs):
            result = original(root, data, **kwargs)
            records.append(json.loads(json.dumps(data)))
            return result
        return records, patch.object(books, '_save_journal', side_effect=record)

    def _crash_after(self, original, ordinal):
        state = {'count': 0}
        def wrapper(*args, **kwargs):
            if args and isinstance(args[0], str) and args[0].startswith('.exchange-probe-'):
                return original(*args, **kwargs)
            result = original(*args, **kwargs)
            state['count'] += 1
            if state['count'] == ordinal:
                raise _Crash(f'crash after call {ordinal}')
            return result
        return wrapper

    def _crash_before(self, original, ordinal):
        state = {'count': 0}
        def wrapper(*args, **kwargs):
            if args and isinstance(args[0], str) and args[0].startswith('.exchange-probe-'):
                return original(*args, **kwargs)
            state['count'] += 1
            if state['count'] == ordinal:
                raise _Crash(f'crash before call {ordinal}')
            return original(*args, **kwargs)
        return wrapper

    def _disable_forward_recovery(self):
        """Emulate process death instead of the handler's in-process recovery."""
        return patch.object(books, 'recover_book_state_locked', lambda *a, **k: None)

    def _disable_prepublication_cleanup(self):
        """Leave a just-written publishing journal exactly as a crash would."""
        return (
            patch.object(books, '_retire_unpublished_journal',
                         lambda root, data, version, root_fd, targets=None: version),
            patch.object(books, '_cleanup_unpublished_stream_workspace', lambda *a, **k: None),
            patch.object(books, '_finish_transaction', lambda *a, **k: None),
            patch.object(books, '_unlink_owned_book_journal', lambda *a, **k: None),
        )

    def _crash_apply(self, *, prepublication_cleanup=False, streamed=False):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(self._disable_forward_recovery())
            if prepublication_cleanup:
                for manager in self._disable_prepublication_cleanup():
                    stack.enter_context(manager)
            with self.assertRaises(_Crash):
                self._apply(self._replacement_values(streamed=streamed), streamed=streamed)

    def _assert_recovered(self, expected):
        for _repeat in range(2):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(expected, _snapshot(self.root))
        self.assertFalse((self.root / books.JOURNAL).exists())

    # --- forward path -----------------------------------------------------

    def test_replacement_only_transaction_is_v4_and_target_is_never_absent(self):
        originals = self._prepare()
        new = {name: f'replacement-{index}'.encode()
               for index, name in enumerate(originals)}
        records, recorder = self._capture_journals()
        seen = []
        real = publication.exchange_source_upload_names
        def observe_both(source, destination, *, source_dir_fd=None, destination_dir_fd=None):
            if str(source).startswith('.exchange-probe-'):
                return real(source, destination, source_dir_fd=source_dir_fd,
                            destination_dir_fd=destination_dir_fd)
            before = os.stat(source, dir_fd=source_dir_fd, follow_symlinks=False)
            result = real(source, destination, source_dir_fd=source_dir_fd,
                          destination_dir_fd=destination_dir_fd)
            after = os.stat(source, dir_fd=source_dir_fd, follow_symlinks=False)
            seen.append((before.st_ino, after.st_ino, before.st_dev == after.st_dev))
            return result
        with recorder, patch.object(publication, 'exchange_source_upload_names',
                                    side_effect=observe_both):
            self._apply(new)
        self.assertEqual([4, 4], [record['version'] for record in records])
        self.assertEqual(['publishing', 'committed'],
                         [record['phase'] for record in records])
        self.assertEqual(2, len(seen))
        for before_ino, after_ino, same_device in seen:
            self.assertNotEqual(before_ino, after_ino)   # the name was never absent
            self.assertTrue(same_device)
        expected = {name: new[name] for name in originals}
        self.assertEqual(expected, _snapshot(self.root))

    def test_streamed_replacement_only_transaction_is_v4(self):
        originals = self._prepare()
        new = self._replacement_values(streamed=True)
        records, recorder = self._capture_journals()
        with recorder:
            self._apply(new, streamed=True)
        self.assertEqual([4, 4], [record['version'] for record in records])
        self.assertIn('stream_limits', records[0])
        self.assertEqual({name: f'replacement-{index}'.encode()
                          for index, name in enumerate(originals)},
                         _snapshot(self.root))

    def test_mixed_transaction_stays_on_v3_and_never_exchanges(self):
        originals = self._prepare()
        records, recorder = self._capture_journals()
        def refuse_exchange(*args, **kwargs):
            raise AssertionError('a mixed transaction must not exchange')
        with recorder, patch.object(publication, 'exchange_source_upload_names',
                                    side_effect=refuse_exchange):
            self._apply({'part-0.json': b'new-0'},
                        removals=['part-1.json'])
        self.assertEqual({3, 3}, {record['version'] for record in records})
        self.assertEqual(b'new-0', (self.root / 'part-0.json').read_bytes())
        self.assertFalse((self.root / 'part-1.json').exists())

    def test_creation_row_keeps_v3_even_when_other_rows_are_replacements(self):
        self._prepare()
        records, recorder = self._capture_journals()
        def refuse_exchange(*args, **kwargs):
            raise AssertionError('a transaction with a creation must not exchange')
        with recorder, patch.object(publication, 'exchange_source_upload_names',
                                    side_effect=refuse_exchange):
            self._apply({'part-0.json': b'new-0', 'brand-new.json': b'created'})
        self.assertEqual({3, 3}, {record['version'] for record in records})

    def test_unsupported_filesystem_stays_on_v3(self):
        self._prepare()
        records, recorder = self._capture_journals()
        def refuse_exchange(*args, **kwargs):
            raise AssertionError('an unsupported probe must not exchange')
        with recorder, \
             patch.object(publication, 'probe_exchange_support', return_value=False), \
             patch.object(publication, 'exchange_source_upload_names',
                          side_effect=refuse_exchange):
            self._apply({'part-0.json': b'new-0', 'part-1.json': b'new-1'})
        self.assertEqual({3, 3}, {record['version'] for record in records})
        self.assertEqual({3, 3}, {record['version'] for record in records})

    # --- crash-state enumeration -----------------------------------------

    def test_crash_after_journal_write_rolls_back_to_originals(self):
        originals = self._prepare()
        original = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply(prepublication_cleanup=True)
        self.assertTrue((self.root / books.JOURNAL).exists())
        self._assert_recovered(originals)

    def test_crash_before_each_exchange_rolls_back(self):
        originals = self._prepare()
        for ordinal in (1, 2):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.root)
                self.root.mkdir()
                originals = self._prepare()
                original = publication.exchange_source_upload_names
                with patch.object(publication, 'exchange_source_upload_names',
                                  side_effect=self._crash_before(original, ordinal)):
                    self._crash_apply()
                self._assert_recovered(originals)

    def test_crash_after_each_exchange_rolls_back(self):
        for ordinal in (1, 2):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.root)
                self.root.mkdir()
                originals = self._prepare()
                original = publication.exchange_source_upload_names
                with patch.object(publication, 'exchange_source_upload_names',
                                  side_effect=self._crash_after(original, ordinal)):
                    self._crash_apply()
                self._assert_recovered(originals)

    def test_streamed_crash_after_each_exchange_rolls_back(self):
        for ordinal in (1, 2):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.root)
                self.root.mkdir()
                originals = self._prepare()
                original = publication.exchange_source_upload_names
                with patch.object(publication, 'exchange_source_upload_names',
                                  side_effect=self._crash_after(original, ordinal)):
                    self._crash_apply(streamed=True)
                self._assert_recovered(originals)

    def test_crash_after_commit_write_finishes_new_book(self):
        originals = self._prepare()
        new = {name: f'replacement-{index}'.encode()
               for index, name in enumerate(originals)}
        original = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(original, 2)):
            self._crash_apply()
        self.assertTrue((self.root / books.JOURNAL).exists())
        self._assert_recovered(new)

    def test_crash_before_committed_cleanup_finishes_new_book(self):
        originals = self._prepare()
        new = {name: f'replacement-{index}'.encode()
               for index, name in enumerate(originals)}
        with patch.object(books, '_cleanup_validated_byte_workspace',
                          side_effect=_Crash('crash during cleanup')):
            self._crash_apply()
        self.assertTrue((self.root / books.JOURNAL).exists())
        self._assert_recovered(new)

    def test_crash_after_cleanup_before_journal_unlink_finishes_new_book(self):
        originals = self._prepare()
        new = {name: f'replacement-{index}'.encode()
               for index, name in enumerate(originals)}
        with patch.object(books, '_unlink_owned_book_journal',
                          side_effect=_Crash('crash before unlink')):
            self._crash_apply()
        self.assertTrue((self.root / books.JOURNAL).exists())
        self._assert_recovered(new)

    def test_crash_during_rollback_is_reenetered(self):
        originals = self._prepare()
        original = publication.exchange_source_upload_names
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply()
        # Now crash the recovery's one complementary exchange after it happens.
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after(original, 1)):
            with self.assertRaises(_Crash):
                with books.ensure_book_state(str(self.root)):
                    pass
        self._assert_recovered(originals)

    def test_crash_after_recovered_phase_write_is_reenetered(self):
        originals = self._prepare()
        original = publication.exchange_source_upload_names
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply()
        save = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(save, 1)):
            with self.assertRaises(_Crash):
                with books.ensure_book_state(str(self.root)):
                    pass
        self._assert_recovered(originals)

    # --- writer interference ---------------------------------------------

    def test_writer_substituting_a_target_between_proof_and_exchange_rolls_back(self):
        self._prepare()
        passed = f'replacement-0'.encode()
        sentinel = self.root / 'writer-original'
        real = publication.exchange_source_upload_names
        state = {'count': 0}
        def substitute_then_exchange(source, destination, *, source_dir_fd=None,
                                     destination_dir_fd=None):
            if str(source).startswith('.exchange-probe-'):
                return real(source, destination, source_dir_fd=source_dir_fd,
                            destination_dir_fd=destination_dir_fd)
            state['count'] += 1
            if state['count'] == 1:
                # Move the journaled original aside (never unlink+recreate: CI
                # may reuse the inode number) and put a foreign object at T.
                os.rename(source, sentinel.name, src_dir_fd=source_dir_fd,
                          dst_dir_fd=source_dir_fd)
                write_fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                   0o600, dir_fd=source_dir_fd)
                try:
                    os.write(write_fd, b'writer-foreign')
                finally:
                    os.close(write_fd)
            return real(source, destination, source_dir_fd=source_dir_fd,
                        destination_dir_fd=destination_dir_fd)
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=substitute_then_exchange):
            with self.assertRaises(ValueError) as raised:
                self._apply({'part-0.json': passed, 'part-1.json': b'replacement-1'})
        # The exchange diverged and was undone; recovery then refuses, because
        # a writer's object at a target is the operator's decision (#1660).
        diverged = raised.exception
        while diverged is not None and not isinstance(diverged, books.BookExchangeDiverged):
            diverged = diverged.__cause__
        self.assertIsNotNone(diverged)
        self.assertIsNotNone(diverged.undone)
        # Every object survives: the writer's object at the target, the
        # original at the name the writer moved it to, and nothing adopted.
        self.assertEqual(b'writer-foreign', (self.root / 'part-0.json').read_bytes())
        self.assertEqual(b'original-0', sentinel.read_bytes())
        self.assertEqual(b'original-1', (self.root / 'part-1.json').read_bytes())
        # Not retired, not auto-quarantined: the journal and workspace stay.
        self.assertTrue((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob(books.QUARANTINE_PREFIX + '*')))
        with self.assertRaises(ValueError):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(b'writer-foreign', (self.root / 'part-0.json').read_bytes())
        # The operator keeps the current files; nothing is deleted.
        import book_state_recovery
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(0, book_state_recovery.main(
                ['--data-dir', str(self.root), 'quarantine', '--accept-current-files']))
        self.assertEqual(b'writer-foreign', (self.root / 'part-0.json').read_bytes())
        [quarantine] = list(self.root.glob(books.QUARANTINE_PREFIX + '*'))
        self.assertEqual(b'replacement-0',
                         (quarantine / books.QUARANTINE_WORKSPACE / 'new-0').read_bytes())

    def test_writer_replacing_the_published_target_keeps_the_original(self):
        self._prepare()
        real = publication.exchange_source_upload_names
        sentinel = self.root / 'published-original'
        state = {'count': 0}
        def edit_after_exchange(source, destination, *, source_dir_fd=None,
                                destination_dir_fd=None):
            result = real(source, destination, source_dir_fd=source_dir_fd,
                          destination_dir_fd=destination_dir_fd)
            if str(source).startswith('.exchange-probe-'):
                return result
            state['count'] += 1
            if state['count'] == 1:
                # The swap is done (T=N, slot=O); replace T again.
                os.rename(source, sentinel.name, src_dir_fd=source_dir_fd,
                          dst_dir_fd=source_dir_fd)
                write_fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                   0o600, dir_fd=source_dir_fd)
                try:
                    os.write(write_fd, b'writer-after-swap')
                finally:
                    os.close(write_fd)
            return result
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=edit_after_exchange):
            with self.assertRaises(books.BookExchangeDiverged):
                self._apply({'part-0.json': b'replacement-0',
                             'part-1.json': b'replacement-1'})
        # The swap is undone (T=O, slot=F), then recovery puts the original
        # back at the target; the writer's later object stays in the
        # quarantine workspace and N is preserved at the writer's own name.
        self.assertEqual(b'original-0', (self.root / 'part-0.json').read_bytes())
        self.assertEqual(b'replacement-0', sentinel.read_bytes())
        self.assertFalse((self.root / books.JOURNAL).exists())
        quarantines = [entry for entry in self.root.iterdir()
                       if entry.name.startswith(books.QUARANTINE_PREFIX)]
        self.assertEqual(1, len(quarantines))
        self.assertEqual(b'writer-after-swap',
                         (quarantines[0] / 'workspace' / 'new-0').read_bytes())

    # --- journal schema, quarantine, operator command --------------------

    def _valid_v4_journal(self, values):
        rows = []
        for index, name in enumerate(sorted(values)):
            target = self.root / name
            info = target.lstat()
            rows.append({
                'path': name, 'existed': True,
                'old_sha256': _sha(target.read_bytes()),
                'new_sha256': _sha(values[name]),
                'old_file_identity': [info.st_dev, info.st_ino, info.st_size,
                                      info.st_mtime_ns, info.st_mode, info.st_nlink],
                'new_file_identity': [info.st_dev + 1, info.st_ino + 1],
            })
        return {'version': 4, 'phase': 'publishing',
                'workspace': '.book-switch-' + 'a' * 32,
                'workspace_identity': [1, 2], 'entries': rows}

    def test_v4_journal_schema_accepts_only_replacement_rows(self):
        originals = self._prepare()
        values = {name: f'replacement-{index}'.encode()
                  for index, name in enumerate(originals)}
        data = self._valid_v4_journal(values)
        workspace, _identity, rows, _names, limits, _parents = (
            books._validate_recovery_journal_shape(self.root, self.root / books.JOURNAL, data))
        self.assertEqual(2, len(rows))
        self.assertIsNone(limits)
        cases = {
            'placeholder on a replacement': {'placeholder': {
                'kind': 'file', 'nonce': '0' * 32, 'identity': [1, 2], 'generation': None}},
            'creation row': {'existed': False, 'old_sha256': None,
                             'old_file_identity': None},
            'removal row': {'new_sha256': None, 'new_file_identity': None},
            'missing original identity': {'old_file_identity': None},
        }
        for label, change in cases.items():
            with self.subTest(label=label):
                broken = json.loads(json.dumps(data))
                broken['entries'][0].update(change)
                if label == 'placeholder on a replacement':
                    pass
                with self.assertRaises(ValueError):
                    books._validate_recovery_journal_shape(
                        self.root, self.root / books.JOURNAL, broken)

    def test_v4_journal_is_learned_by_status_inspect_and_operator_command(self):
        originals = self._prepare()
        original = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply(prepublication_cleanup=True)
        journal = self.root / books.JOURNAL
        self.assertTrue(journal.exists())
        self.assertEqual(4, json.loads(journal.read_text())['version'])
        self.assertEqual('valid', books.get_book_journal_status(self.root))
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual(4, report['journal']['version'])
        self.assertEqual('publishing', report['journal']['phase'])
        result = subprocess.run(
            [sys.executable, 'book_state_recovery.py', '--data-dir', str(self.root),
             '--json', 'inspect', '--no-lock'],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(0, payload['exit_code'])
        self.assertEqual(4, payload['report']['journal']['version'])
        finish = subprocess.run(
            [sys.executable, 'book_state_recovery.py', '--data-dir', str(self.root),
             '--json', 'finish'],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, finish.returncode, finish.stderr)
        self.assertFalse(journal.exists())
        self.assertEqual(originals, _snapshot(self.root))

    def test_v4_publishing_journal_auto_quarantines_unowned_workspace_data(self):
        originals = self._prepare()
        original = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply(prepublication_cleanup=True)
        workspace = next(self.root.glob('.book-switch-*'))
        (workspace / 'foreign-evidence').write_bytes(b'writer-evidence')
        with books.ensure_book_state(str(self.root)):
            pass
        quarantines = [entry for entry in self.root.iterdir()
                       if entry.name.startswith(books.QUARANTINE_PREFIX)]
        self.assertEqual(1, len(quarantines))
        receipt = json.loads((quarantines[0] / books.QUARANTINE_RECEIPT).read_text())
        self.assertEqual('completed', receipt['state'])
        self.assertEqual(4, receipt['journal']['version'])
        self.assertEqual(b'writer-evidence',
                         (quarantines[0] / 'workspace' / 'foreign-evidence').read_bytes())
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual(originals, _snapshot(self.root))

    def test_forced_operator_quarantine_understands_v4(self):
        self._prepare()
        original = books._save_journal
        with patch.object(books, '_save_journal',
                          side_effect=self._crash_after(original, 1)):
            self._crash_apply(prepublication_cleanup=True)
        result = subprocess.run(
            [sys.executable, 'book_state_recovery.py', '--data-dir', str(self.root),
             '--json', 'quarantine', '--accept-current-files'],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.root / books.JOURNAL).exists())
        quarantines = [entry for entry in self.root.iterdir()
                       if entry.name.startswith(books.QUARANTINE_PREFIX)]
        self.assertEqual(1, len(quarantines))
        receipt = json.loads((quarantines[0] / books.QUARANTINE_RECEIPT).read_text())
        self.assertEqual(4, receipt['journal']['version'])


    # --- review of #1880: evidence is kept, never retired silently --------

    def _journal(self):
        return json.loads((self.root / books.JOURNAL).read_text())

    def _workspace(self):
        return self.root / self._journal()['workspace']

    def _crash_after_exchanges(self, count):
        original = publication.exchange_source_upload_names
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after(original, count)):
            self._crash_apply()

    def _assert_refused_with_evidence(self, message):
        before = self._journal()
        for _repeat in range(2):
            with self.assertRaisesRegex(ValueError, message):
                with books.ensure_book_state(str(self.root)):
                    pass
        self.assertEqual(before, self._journal())   # journal kept, phase unchanged

    def test_a_target_deleted_before_any_exchange_is_refused_not_retired(self):
        self._prepare()
        original = publication.exchange_source_upload_names
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_before(original, 1)):
            self._crash_apply()
        staged = self._workspace() / 'new-0'
        (self.root / 'part-0.json').unlink()   # someone deletes it; its only copy
        self._assert_refused_with_evidence('could not restore every original')
        self.assertEqual(b'replacement-0', staged.read_bytes())   # nothing of ours deleted
        self.assertEqual(b'original-1', (self.root / 'part-1.json').read_bytes())

    def test_a_target_deleted_after_the_workspace_went_keeps_the_journal(self):
        originals = self._prepare()
        self._crash_after_exchanges(1)
        # Recovery rolls back and removes the workspace, then dies before the
        # journal is unlinked: a 'recovered' journal with no workspace.
        with patch.object(books, '_unlink_owned_book_journal',
                          side_effect=_Crash('crash before unlink')), self.assertRaises(_Crash):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual('recovered', self._journal()['phase'])
        self.assertFalse(self._workspace().exists())
        (self.root / 'part-0.json').rename(self.root / 'kept-aside')
        self._assert_refused_with_evidence('')
        (self.root / 'kept-aside').rename(self.root / 'part-0.json')
        self._assert_recovered(originals)   # once intact, it retires as before

    def test_a_failed_commit_proof_leaves_the_journal_publishing(self):
        self._prepare()
        self._crash_after_exchanges(2)   # both published; slots hold the originals
        workspace = self._workspace()
        for name in ('new-0', 'new-1'):
            (workspace / name).rename(self.root / f'{name}-kept')
        (self.root / 'part-0.json').write_bytes(b'edited in place')
        self._assert_refused_with_evidence('')
        self.assertEqual('publishing', self._journal()['phase'])

    def test_a_published_row_beside_an_unpublished_one_is_never_committed(self):
        self._prepare()
        self._crash_after_exchanges(1)   # row 0 published, row 1 not
        (self._workspace() / 'new-0').rename(self.root / 'original-0-kept')
        self._assert_refused_with_evidence('cannot be completed consistently')
        self.assertEqual(b'replacement-0', (self.root / 'part-0.json').read_bytes())
        self.assertEqual(b'original-1', (self.root / 'part-1.json').read_bytes())

    def test_a_rollback_that_leaves_a_replacement_in_place_is_refused(self):
        self._prepare()
        self._crash_after_exchanges(2)
        with patch.object(books, '_undo_book_exchange', lambda *a, **k: None):
            self._assert_refused_with_evidence('could not restore every original')
        self.assertEqual(b'replacement-0', (self.root / 'part-0.json').read_bytes())

    def test_a_changed_rotated_slot_is_never_deleted(self):
        self._prepare()
        with patch.object(books, '_cleanup_validated_byte_workspace',
                          side_effect=_Crash('crash during cleanup')):
            self._crash_apply()
        self.assertEqual('committed', self._journal()['phase'])
        slot = self._workspace() / 'new-0'   # holds the original after the swap
        info = slot.stat()
        slot.write_bytes(b'ORIGINAL-0')   # same size, different bytes
        os.utime(slot, ns=(info.st_atime_ns, info.st_mtime_ns))   # same identity fields
        with books.ensure_book_state(str(self.root)):
            pass   # auto-quarantine keeps the slot it cannot prove
        kept = [p for p in self.root.rglob('new-0') if p.read_bytes() == b'ORIGINAL-0']
        self.assertEqual(1, len(kept))
        self.assertTrue(any(part.startswith(books.QUARANTINE_PREFIX) for part in kept[0].parts))

    def test_a_slot_swapped_before_the_undo_is_never_moved_to_the_target(self):
        originals = self._prepare()
        self._crash_after_exchanges(1)
        slots = books._get_book_workspace_slots
        def swap_after_classifying(workspace, index, row):
            result = slots(workspace, index, row)
            if index == 0 and result[0] is not None:
                (workspace / result[0]).rename(self.root / 'real-original-0')
                (workspace / result[0]).write_bytes(b'impostor')
            return result
        with patch.object(books, '_get_book_workspace_slots', side_effect=swap_after_classifying):
            with self.assertRaises(ValueError):
                with books.ensure_book_state(str(self.root)):
                    pass
        self.assertNotEqual(b'impostor', (self.root / 'part-0.json').read_bytes())

    def test_exchange_lost_at_recovery_names_the_way_out(self):
        self._prepare()
        self._crash_after_exchanges(1)
        import errno
        before = _snapshot(self.root)
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=OSError(errno.ENOTSUP, 'no exchange')):
            with self.assertRaisesRegex(OSError, 'quarantine --accept-current-files'):
                with books.ensure_book_state(str(self.root)):
                    pass
        self.assertEqual(before, _snapshot(self.root))
        self.assertTrue((self.root / books.JOURNAL).exists())
        # The way out works without exchange: the operator keeps the current files.
        import book_state_recovery
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=OSError(errno.ENOTSUP, 'no exchange')), \
                patch('sys.stdout', new_callable=io.StringIO):
            code = book_state_recovery.main(['--data-dir', str(self.root),
                                             'quarantine', '--accept-current-files'])
        self.assertEqual(0, code)
        self.assertFalse((self.root / books.JOURNAL).exists())

    def test_a_target_deleted_after_its_swap_is_restored_from_its_proven_original(self):
        # R8, as on v2/v3: the original is proven in our slot, so it goes back
        # with a no-overwrite move and the switch rolls back completely.
        originals = self._prepare()
        self._crash_after_exchanges(1)   # row 0 published, its original in new-0
        (self.root / 'part-0.json').rename(self.root / 'removed-by-someone')
        self._assert_recovered({**originals, 'removed-by-someone': b'replacement-0'})

    def test_an_edit_made_after_classification_is_never_swapped_out(self):
        # Every target is proven at classification, but the undo of row 0
        # runs before row 1's: an in-place edit to row 1 in between keeps its
        # inode, so only the re-proof right before row 1's undo can see it.
        self._prepare()
        self._crash_after_exchanges(2)   # both published
        undo, done = books._undo_book_exchange, []
        def edit_the_next_target_first(*args, **kwargs):
            result = undo(*args, **kwargs)
            if not done:
                (self.root / 'part-1.json').write_bytes(b'writer edit-1')   # same inode
                done.append(True)
            return result
        with patch.object(books, '_undo_book_exchange', side_effect=edit_the_next_target_first):
            with self.assertRaisesRegex(ValueError, 'A book file changed during the switch'):
                with books.ensure_book_state(str(self.root)):
                    pass
        self.assertEqual(b'writer edit-1', (self.root / 'part-1.json').read_bytes())
        self.assertTrue((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob(books.QUARANTINE_PREFIX + '*')))

if __name__ == '__main__':
    unittest.main()
