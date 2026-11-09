"""#1660 (d): a crash between removing an unpublished workspace and retiring
its journal must leave state that recovery completes, not refuses forever."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
from tests.test_book_state_streaming import stream


class _Crash(BaseException):
    """Process death: nothing in the transaction may catch or clean up after it."""


class PrepublicationCrashTests(unittest.TestCase):
    """A transaction that fails after its journal is published but before any
    target moves, for stream (v2) and byte (v3) journals, on every platform."""

    def fail_before_first_move(self, root, kind, *, tamper=None):
        """Run a stream or byte transaction that fails after publishing its
        journal and before moving a target. `tamper` edits the original first."""
        if kind == 'stream':
            save, cancelled = books._save_journal, [False]
            def after_save(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing':
                    if tamper:
                        tamper()
                    cancelled[0] = True
                return result
            trigger = patch.object(books, '_save_journal', side_effect=after_save)
            run = lambda: books.apply_book_state_locked(
                root, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100,
                cancel_check=lambda: cancelled[0])
        else:
            def staged_check(*args, **kwargs):
                if tamper:
                    tamper()
                raise ValueError('staged replacement changed')
            # The first staged check runs after publication, before any move.
            trigger = patch.object(books, '_validate_staged_replacement', side_effect=staged_check)
            run = lambda: books.apply_book_state_locked(root, {'audio.wav': b'new'}, [])
        return trigger, run

    def phase(self, root):
        return json.loads((root / books.JOURNAL).read_text(encoding='utf-8'))['phase']

    def recover(self, root, target, original):
        with books.ensure_book_state(root):   # startup / next admission recovers
            pass
        self.assertFalse((root / books.JOURNAL).exists())
        self.assertEqual([], list(root.glob('.book-switch-*')))
        self.assertEqual(original, target.read_bytes())

    def test_crash_after_workspace_removal_before_journal_unlink_recovers(self):
        for kind in ('stream', 'byte'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'audio.wav'
                target.write_bytes(b'old audio')
                trigger, run = self.fail_before_first_move(root, kind)
                def crash(*args, **kwargs):
                    raise _Crash()
                with trigger, patch.object(books, '_unlink_owned_book_journal', side_effect=crash), \
                        self.assertRaises(_Crash):
                    with books.ensure_book_state(root):
                        run()
                # The crash left the journal, workspace already removed.
                self.assertEqual('recovered', self.phase(root))
                self.assertEqual([], list(root.glob('.book-switch-*')))
                self.recover(root, target, b'old audio')

    def test_crash_after_recovered_save_with_workspace_present_recovers(self):
        for kind in ('stream', 'byte'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'audio.wav'
                target.write_bytes(b'old audio')
                trigger, run = self.fail_before_first_move(root, kind)
                retire = books._retire_unpublished_journal
                def crash_after_retire(*args, **kwargs):
                    retire(*args, **kwargs)
                    raise _Crash()
                with trigger, patch.object(books, '_retire_unpublished_journal',
                                           side_effect=crash_after_retire), self.assertRaises(_Crash):
                    with books.ensure_book_state(root):
                        run()
                self.assertEqual('recovered', self.phase(root))
                self.assertEqual(1, len(list(root.glob('.book-switch-*'))))
                self.recover(root, target, b'old audio')

    def test_changed_original_is_never_marked_recovered(self):
        # "Recovered" must never be claimed over an original another writer
        # changed. Each kind keeps its previous outcome for that case.
        for kind in ('stream', 'byte'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'audio.wav'
                target.write_bytes(b'old audio')
                trigger, run = self.fail_before_first_move(
                    root, kind, tamper=lambda: target.write_bytes(b'concurrent edit'))
                with trigger, self.assertRaisesRegex(ValueError, 'changed'):
                    with books.ensure_book_state(root):
                        run()
                if kind == 'stream':
                    # Unchanged: the publishing journal stays as evidence.
                    self.assertEqual('publishing', self.phase(root))
                else:
                    # Unchanged: the byte cleanup retires its journal and lets the edit stand.
                    self.assertFalse((root / books.JOURNAL).exists())
                    self.assertEqual([], list(root.glob('.book-switch-*')))
                self.assertEqual(b'concurrent edit', target.read_bytes())


if __name__ == '__main__':
    unittest.main()
