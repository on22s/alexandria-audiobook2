"""#1660 auto-quarantine: a switch recovery refuses while every target is
consistent is moved aside, never deleted, instead of leaving the book stuck."""
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
from tests.test_support import get_quarantined_path


class _Crash(BaseException):
    """Process death: the transaction's own cleanup must not run."""


def stream(data):
    return books.BookStateStream(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


class AutoQuarantineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.target = self.root / 'state.json'

    def interrupted_switch(self, *, streams=False):
        """A publishing journal with new-0 staged and the original untouched."""
        self.target.write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                if streams:
                    books.apply_book_state_locked(self.root, {'state.json': stream(b'new')}, [],
                                                  stream_byte_limit=100)
                else:
                    books.apply_book_state_locked(self.root, {'state.json': b'new'}, [])
        journal = json.loads((self.root / books.JOURNAL).read_text())
        self.assertEqual('publishing', journal['phase'])
        return self.root / journal['workspace']

    def quarantines(self):
        return sorted(entry for entry in self.root.iterdir()
                      if entry.name.startswith(books.QUARANTINE_PREFIX))

    def tree(self, directory):
        return {str(path.relative_to(directory)): None if path.is_dir() else path.read_bytes()
                for path in sorted(directory.rglob('*'))}

    def test_changed_stream_stage_is_quarantined_whole(self):
        workspace = self.interrupted_switch(streams=True)
        (workspace / 'new-0').write_bytes(b'foreign')
        (workspace / 'note').write_bytes(b'user data')
        before, identity = self.tree(workspace), workspace.lstat()
        with books.ensure_book_state(self.root):
            pass
        moved = get_quarantined_path(self, self.root, workspace / 'new-0').parent
        self.assertEqual(before, self.tree(moved))   # nothing deleted
        after = moved.lstat()   # renamed, not copied
        self.assertEqual((identity.st_dev, identity.st_ino), (after.st_dev, after.st_ino))
        [quarantine] = self.quarantines()
        raw = (quarantine / books.QUARANTINE_JOURNAL).read_bytes()
        # Recovery restored nothing and recorded that before refusing cleanup.
        self.assertEqual('recovered', json.loads(raw)['phase'])
        self.assertTrue(quarantine.name.startswith(
            books.QUARANTINE_PREFIX + hashlib.sha256(raw).hexdigest()[:8] + '-'))
        self.assertEqual(b'old', self.target.read_bytes())

    def test_publishing_switch_whose_workspace_vanished_is_quarantined(self):
        workspace = self.interrupted_switch()
        shutil.rmtree(workspace)
        with self.assertRaisesRegex(ValueError, 'workspace changed'), \
                patch.object(books, '_quarantine_book_transaction', return_value=False):
            with books.ensure_book_state(self.root):
                pass   # what main does: stuck for good
        with books.ensure_book_state(self.root):
            pass
        [quarantine] = self.quarantines()
        receipt = json.loads((quarantine / books.QUARANTINE_RECEIPT).read_text())
        self.assertEqual(('completed', None), (receipt['state'], receipt['workspace']['identity']))
        self.assertFalse((quarantine / books.QUARANTINE_WORKSPACE).exists())
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual(b'old', self.target.read_bytes())

    def test_a_changed_target_is_never_quarantined(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        self.target.write_bytes(b'someone else')
        for _attempt in range(2):
            with self.assertRaises(ValueError):
                with books.ensure_book_state(self.root):
                    pass
        self.assertEqual([], self.quarantines())
        self.assertTrue((self.root / books.JOURNAL).exists())
        self.assertEqual(b'foreign', (workspace / 'new-0').read_bytes())

    def test_malformed_and_legacy_journals_are_never_quarantined(self):
        workspace = self.root / ('.book-switch-' + 'a' * 32)
        workspace.mkdir()
        legacy = {'version': 1, 'phase': 'committed', 'workspace': workspace.name,
                  'entries': [{'path': 'state.json', 'existed': False,
                               'old_sha256': None, 'new_sha256': None}]}
        for raw in (b'not json', json.dumps(legacy).encode()):
            with self.subTest(raw=raw[:8]):
                (self.root / books.JOURNAL).write_bytes(raw)
                with self.assertRaises(ValueError):
                    with books.ensure_book_state(self.root):
                        pass
                self.assertEqual([], self.quarantines())
                self.assertEqual(raw, (self.root / books.JOURNAL).read_bytes())

    def test_cancellation_while_deciding_quarantines_nothing(self):
        from audio_generation_owner import AudioGenerationCancelled
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        refuse = books._recover_book_journal
        calls = []
        def cancel():
            return bool(calls)
        def refused(*args, **kwargs):
            calls.append(1)
            return refuse(*args, **kwargs)
        with patch.object(books, '_recover_book_journal', side_effect=refused), \
                self.assertRaises(AudioGenerationCancelled):
            with books.ensure_book_state(self.root, cancel_check=cancel):
                pass
        self.assertEqual([], self.quarantines())
        self.assertTrue(workspace.exists())

    def test_crash_after_the_workspace_move_resumes_into_the_same_quarantine(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        move = books._move
        def die_on_journal(source, destination, **kwargs):
            if Path(source).name == books.JOURNAL:
                raise _Crash()
            return move(source, destination, **kwargs)
        with patch.object(books, '_move', side_effect=die_on_journal), self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        [quarantine] = self.quarantines()
        self.assertTrue((quarantine / books.QUARANTINE_WORKSPACE / 'new-0').exists())
        self.assertTrue((self.root / books.JOURNAL).exists())
        # Normal recovery would now retire the journal of an absent workspace;
        # the pending receipt must win.
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual([quarantine], self.quarantines())
        self.assertEqual(b'foreign', get_quarantined_path(self, self.root, workspace / 'new-0').read_bytes())

    def test_crash_before_any_move_resumes_into_the_same_quarantine(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        with patch.object(books, '_move', side_effect=_Crash()), self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        [quarantine] = self.quarantines()
        self.assertEqual('prepared', json.loads(
            (quarantine / books.QUARANTINE_RECEIPT).read_text())['state'])
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual([quarantine], self.quarantines())
        self.assertEqual(b'foreign', get_quarantined_path(self, self.root, workspace / 'new-0').read_bytes())

    def test_crash_before_the_completed_receipt_still_admits_the_book(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        write = books._write_quarantine_receipt
        def die_on_completion(directory, receipt):
            if receipt['state'] == 'completed':
                raise _Crash()
            return write(directory, receipt)
        with patch.object(books, '_write_quarantine_receipt', side_effect=die_on_completion), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        with books.ensure_book_state(self.root):
            pass
        [quarantine] = self.quarantines()
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual(b'foreign', (quarantine / books.QUARANTINE_WORKSPACE / 'new-0').read_bytes())

    def test_a_directory_left_before_its_receipt_is_kept_and_not_reused(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        sha8 = hashlib.sha256((self.root / books.JOURNAL).read_bytes()).hexdigest()[:8]
        inert = self.root / f'{books.QUARANTINE_PREFIX}{sha8}-00000000'
        inert.mkdir()
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual(2, len(self.quarantines()))
        self.assertEqual([], list(inert.iterdir()))

    def test_two_pending_quarantines_for_one_journal_are_refused(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        with patch.object(books, '_move', side_effect=_Crash()), self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        [first] = self.quarantines()
        twin = self.root / (first.name[:-8] + 'ffffffff')
        shutil.copytree(first, twin)
        with self.assertRaisesRegex(ValueError, 'More than one'):
            with books.ensure_book_state(self.root):
                pass
        self.assertTrue((workspace / 'new-0').exists())
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_a_journal_replaced_during_its_move_is_put_back(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        move = books._move
        def substitute(source, destination, **kwargs):
            result = move(source, destination, **kwargs)
            if Path(destination).name == books.QUARANTINE_JOURNAL:
                Path(destination).write_bytes(b'{"forged": 1}')
            return result
        with patch.object(books, '_move', side_effect=substitute), \
                self.assertRaisesRegex(ValueError, 'journal changed during quarantine'):
            with books.ensure_book_state(self.root):
                pass
        self.assertEqual(b'{"forged": 1}', (self.root / books.JOURNAL).read_bytes())
        self.assertEqual(b'foreign', (workspace / 'new-0').read_bytes())
        [quarantine] = self.quarantines()
        self.assertEqual([books.QUARANTINE_RECEIPT], [p.name for p in quarantine.iterdir()])


    def two_entry_switch(self):
        """A publishing journal for two targets, the stage of the first changed."""
        (self.root / 'second.json').write_bytes(b'second')
        self.target.write_bytes(b'old')
        def crash(*args, **kwargs):
            raise _Crash()
        with patch.object(books, '_validate_staged_replacement', side_effect=crash), \
                patch.object(books, '_retire_unpublished_journal', side_effect=crash), \
                self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                books.apply_book_state_locked(
                    self.root, {'state.json': b'new', 'second.json': b'two'}, [])
        workspace = self.root / json.loads((self.root / books.JOURNAL).read_text())['workspace']
        (workspace / 'new-0').write_bytes(b'foreign')
        return workspace

    def test_every_target_is_checked_not_only_the_first(self):
        workspace = self.two_entry_switch()
        (self.root / 'second.json').write_bytes(b'someone else')
        with self.assertRaises(ValueError):
            with books.ensure_book_state(self.root):
                pass
        self.assertEqual([], self.quarantines())
        self.assertTrue(workspace.exists())

    def test_a_consistent_two_target_switch_is_quarantined(self):
        workspace = self.two_entry_switch()
        with books.ensure_book_state(self.root):
            pass
        self.assertEqual(b'foreign', get_quarantined_path(self, self.root, workspace / 'new-0').read_bytes())
        self.assertEqual((b'old', b'second'),
                         (self.target.read_bytes(), (self.root / 'second.json').read_bytes()))

    def test_another_directory_at_the_workspace_name_is_not_adopted(self):
        workspace = self.interrupted_switch()
        os.rename(workspace, self.root / 'elsewhere')
        workspace.mkdir()
        (workspace / 'not ours').write_bytes(b'user data')
        with self.assertRaises(ValueError):
            with books.ensure_book_state(self.root):
                pass
        self.assertEqual([], self.quarantines())
        self.assertEqual(b'user data', (workspace / 'not ours').read_bytes())

    def test_a_receipt_missing_its_workspace_identity_is_refused_not_crashed(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        with patch.object(books, '_move', side_effect=_Crash()), self.assertRaises(_Crash):
            with books.ensure_book_state(self.root):
                pass
        [quarantine] = self.quarantines()
        receipt_path = quarantine / books.QUARANTINE_RECEIPT
        receipt = json.loads(receipt_path.read_text())
        del receipt['workspace']['identity']
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'Invalid book quarantine receipt'):
            with books.ensure_book_state(self.root):
                pass
        self.assertTrue((workspace / 'new-0').exists())

    def test_a_root_replaced_before_the_receipt_gets_nothing_written(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        checks = []
        unchanged = books._is_admitted_root_unchanged
        def replaced_on_second_check(root, identity):
            checks.append(1)
            return unchanged(root, identity) and len(checks) < 2
        with patch.object(books, '_is_admitted_root_unchanged', side_effect=replaced_on_second_check), \
                self.assertRaisesRegex(ValueError, 'Book root changed during quarantine'):
            with books.ensure_book_state(self.root):
                pass
        self.assertEqual([], self.quarantines())
        self.assertTrue((workspace / 'new-0').exists())

    def test_a_directory_swapped_in_during_the_move_is_put_back(self):
        workspace = self.interrupted_switch()
        (workspace / 'new-0').write_bytes(b'foreign')
        move, swapped = books._move, []
        def swap_then_move(source, destination, **kwargs):
            if Path(source) == workspace and not swapped:
                # What a by-name rename does when the name changes underneath it.
                os.rename(workspace, self.root / 'real')
                workspace.mkdir()
                (workspace / 'attacker').write_bytes(b'x')
                swapped.append(1)
                os.rename(workspace, destination)
                return None
            return move(source, destination, **kwargs)
        with patch.object(books, '_move', side_effect=swap_then_move), \
                self.assertRaisesRegex(ValueError, 'workspace changed during quarantine'):
            with books.ensure_book_state(self.root):
                pass
        [quarantine] = self.quarantines()
        self.assertFalse((quarantine / books.QUARANTINE_WORKSPACE).exists())
        self.assertEqual(b'x', (workspace / 'attacker').read_bytes())
        self.assertEqual(b'foreign', (self.root / 'real' / 'new-0').read_bytes())


if __name__ == '__main__':
    unittest.main()
