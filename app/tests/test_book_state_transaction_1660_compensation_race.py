import errno
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
import source_upload_publication as publication
from tests.test_support import assert_book_journal_fenced

OWNED = b'owned original'
GENERATED = b'generated'
FOREIGN = b'foreign replacement'


def _stream(data):
    return books.BookStateStream(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


@unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
class BookStateTransaction1660CompensationRaceTests(unittest.TestCase):
    """Permanent regression for the #1660 residual compensation race.

    On post-move source- or destination-parent authority loss, _move must
    retain files and the journal and raise, never reverse a name that may
    now be foreign. Every case asserts the primary move and parent change
    really happened, that no reverse move was attempted, exact preserved
    names/bytes, an unchanged substitute directory, journal phase, and
    repeated recovery refusal -- a disappearing hook alone cannot pass.
    """

    def test_destination_parent_swap_before_and_after_byte_and_stream(self):
        for streaming in (False, True):
            for timing in ('before', 'after'):
                with self.subTest(streaming=streaming, timing=timing):
                    self._parent_case('destination', streaming, timing)

    def test_source_parent_swap_before_and_after_byte_and_stream(self):
        for streaming in (False, True):
            for timing in ('before', 'after'):
                with self.subTest(streaming=streaming, timing=timing):
                    self._parent_case('source', streaming, timing)

    def test_occupied_source_is_preserved_byte_and_stream(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self._parent_case('destination', streaming, 'occupied')

    def test_direct_directory_move_preserves_on_destination_parent_change(self):
        # Bounded duplication is intentional here: a directory removal has
        # no generated stage and different postconditions (contents, not
        # bytes), so sharing the file harness would hide those oracles.
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                self._directory_case(timing)

    def _run_sync_move(self, sync_raise):
        """Real POSIX _move + relocation; sync_raise fails first post-move fsync."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            retained = root / 'retained'
            real_move, real_fsync = publication.move_source_upload_exclusively, os.fsync
            state = {'moved': False, 'sync_failed': False}
            sync_calls = []
            def relocate(source_name, dest_name, **kwargs):
                result = real_move(source_name, dest_name, **kwargs)
                if not state['moved']:
                    workspace.rename(retained)
                    workspace.mkdir()
                    state['moved'] = True
                return result
            def hooked_sync(fd):
                if state['moved']:
                    sync_calls.append((fd, os.fstat(fd).st_ino))
                    if not state['sync_failed']:
                        state['sync_failed'] = True
                        raise sync_raise
                return real_fsync(fd)
            with patch.object(publication, 'move_source_upload_exclusively', relocate), \
                 patch.object(books.os, 'fsync', hooked_sync):
                try:
                    books._move(source, workspace / 'old-0', exclusive=True, anchor=root)
                except BaseException as error:
                    raised = error
                else:
                    raised = None
            self.assertTrue(state['moved'])
            self.assertTrue(state['sync_failed'])
            self.assertEqual(OWNED, (retained / 'old-0').read_bytes())
            self.assertFalse(source.exists())
            self.assertEqual([], list(workspace.iterdir()))
            return raised, sync_calls, root.stat().st_ino, retained.stat().st_ino

    def test_sync_oserror_preserves_parent_error_as_primary_with_sync_cause(self):
        raised, sync_calls, root_ino, retained_ino = self._run_sync_move(
            OSError(errno.EIO, 'fixture directory sync failure'))
        self.assertIsInstance(raised, ValueError)
        self.assertIn('parent changed', str(raised))
        self.assertIsInstance(raised.__cause__, OSError)
        self.assertIn('fixture directory sync failure', str(raised.__cause__))
        # Both distinct parent fds attempted, source (root) first.
        self.assertGreaterEqual(len(sync_calls), 2)
        self.assertEqual(root_ino, sync_calls[0][1])
        self.assertEqual(retained_ino, sync_calls[1][1])
        self.assertNotEqual(sync_calls[0][0], sync_calls[1][0])

    def test_sync_keyboard_interrupt_is_not_converted_to_value_error(self):
        raised, sync_calls, root_ino, _retained_ino = self._run_sync_move(KeyboardInterrupt())
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertIsInstance(raised.__context__, ValueError)
        self.assertIn('parent changed', str(raised.__context__))
        self.assertEqual(root_ino, sync_calls[0][1])

    def test_sync_oserror_apply_journal_fence_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(OWNED)
            retained = root / 'retained-workspace'
            real_move, real_fsync = publication.move_source_upload_exclusively, os.fsync
            state = {'moved': False, 'sync_failed': False}
            sync_calls = []
            def relocate(source_name, dest_name, **kwargs):
                result = real_move(source_name, dest_name, **kwargs)
                if str(source_name) == 'artifact' and str(dest_name) == 'old-0' and not state['moved']:
                    workspace = next(root.glob('.book-switch-*'))
                    workspace.rename(retained)
                    workspace.mkdir(mode=0o700)
                    state['moved'] = True
                return result
            def fail_sync(fd):
                if state['moved']:
                    sync_calls.append((fd, os.fstat(fd).st_ino))
                    if not state['sync_failed']:
                        state['sync_failed'] = True
                        raise OSError(errno.EIO, 'fixture directory sync failure')
                return real_fsync(fd)
            with patch.object(publication, 'move_source_upload_exclusively', relocate), \
                 patch.object(books.os, 'fsync', fail_sync):
                with books.ensure_book_state(root), self.assertRaises(ValueError) as raised:
                    books.apply_book_state_locked(root, {'artifact': GENERATED}, [])
            self.assertTrue(state['moved'])
            self.assertTrue(state['sync_failed'])
            self.assertGreaterEqual(len(sync_calls), 2)
            self.assertEqual(root.stat().st_ino, sync_calls[0][1])
            self.assertEqual(retained.stat().st_ino, sync_calls[1][1])
            # Recovery failure is primary; its explicit cause is the parent
            # error, whose explicit cause is the first sync OSError.
            self.assertIsInstance(raised.exception.__cause__, ValueError)
            self.assertIn('parent changed', str(raised.exception.__cause__))
            self.assertIsInstance(raised.exception.__cause__.__cause__, OSError)
            self.assertIn('fixture directory sync failure', str(raised.exception.__cause__.__cause__))
            def check():
                self.assertFalse(target.exists())
                self.assertEqual(OWNED, (retained / 'old-0').read_bytes())
                self.assertEqual(GENERATED, (retained / 'new-0').read_bytes())
            assert_book_journal_fenced(self, root, content_check=check)

    def test_ordinary_rollback_with_valid_parents_byte_and_stream(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / 'artifact'
                target.write_bytes(OWNED)
                name = '_validate_moved_stream_original' if streaming else '_validate_moved_byte_original'
                real, calls = getattr(books, name), []
                def fail_once(*args, **kwargs):
                    if not calls:
                        calls.append(True)
                        raise ValueError('fixture moved-original failure')
                    return real(*args, **kwargs)
                value = _stream(GENERATED) if streaming else GENERATED
                options = {'stream_byte_limit': 100} if streaming else {}
                with patch.object(books, name, side_effect=fail_once):
                    with books.ensure_book_state(root), self.assertRaises(ValueError):
                        books.apply_book_state_locked(root, {'artifact': value}, [], **options)
                self.assertEqual([True], calls)
                self.assertEqual(OWNED, target.read_bytes())
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertEqual([], list(root.glob('.book-switch-*')))

    def _parent_case(self, parent, streaming, timing):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            if parent == 'destination':
                target = root / 'artifact'
                retained = root / 'retained-workspace'
                primary, reverse = ('artifact', 'old-0'), ('old-0', 'artifact')
            else:
                sub = root / 'sub'
                sub.mkdir()
                target = sub / 'file.json'
                retained = root / 'retained-sub'
                primary, reverse = ('file.json', 'old-0'), ('old-0', 'file.json')
            target.write_bytes(OWNED)
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False}
            def hooked(source, destination, **kwargs):
                pair = (str(source), str(destination))
                calls.append(pair)
                if pair == primary and not state['relocated']:
                    result = real_move(source, destination, **kwargs)
                    if parent == 'destination':
                        workspace = next(root.glob('.book-switch-*'))
                        workspace.rename(retained)
                        workspace.mkdir(mode=0o700)
                        state['workspace'] = workspace
                        if timing == 'before':
                            (retained / 'old-0').rename(retained / 'owned-retained')
                            (retained / 'old-0').write_bytes(FOREIGN)
                        elif timing == 'after':
                            (retained / 'foreign-stage').write_bytes(FOREIGN)
                        elif timing == 'occupied':
                            (root / 'artifact').write_bytes(FOREIGN)
                    else:
                        sub.rename(retained)
                        sub.mkdir(mode=0o700)
                        workspace = next(root.glob('.book-switch-*'))
                        state['workspace'] = workspace
                        if timing == 'before':
                            (workspace / 'old-0').rename(workspace / 'owned-retained')
                            (workspace / 'old-0').write_bytes(FOREIGN)
                        elif timing == 'after':
                            (workspace / 'foreign-stage').write_bytes(FOREIGN)
                        elif timing == 'occupied':
                            (retained / 'file.json').write_bytes(FOREIGN)
                    state['relocated'] = True
                    return result
                if pair == reverse and timing == 'after':
                    # Attempts the post-observation swap before the real
                    # rename; fail-closed code must never reach here.
                    base = retained if parent == 'destination' else state['workspace']
                    (base / 'old-0').rename(base / 'owned-retained')
                    (base / 'foreign-stage').rename(base / 'old-0')
                return real_move(source, destination, **kwargs)
            value = _stream(GENERATED) if streaming else GENERATED
            options = {'stream_byte_limit': 100} if streaming else {}
            replacements = {'artifact': value} if parent == 'destination' else {'sub/file.json': value}
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, replacements, [], **options)
            self.assertTrue(state['relocated'])
            self.assertIn(primary, calls)
            self.assertNotIn(reverse, calls)
            workspace = state['workspace']
            self.assertTrue(workspace.is_dir())
            if parent == 'destination':
                dest_old, dest_owned = retained / 'old-0', retained / 'owned-retained'
                dest_stage = retained / 'foreign-stage'
                def check():
                    self.assertEqual([], list(workspace.iterdir()))
                    self.assertEqual(GENERATED, (retained / 'new-0').read_bytes())
                    if timing == 'before':
                        self.assertEqual(OWNED, dest_owned.read_bytes())
                        self.assertEqual(FOREIGN, dest_old.read_bytes())
                        self.assertFalse(target.exists())
                    elif timing == 'after':
                        self.assertEqual(OWNED, dest_old.read_bytes())
                        self.assertEqual(FOREIGN, dest_stage.read_bytes())
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(OWNED, dest_old.read_bytes())
                        self.assertEqual(FOREIGN, (root / 'artifact').read_bytes())
            else:
                dest_old, dest_owned = workspace / 'old-0', workspace / 'owned-retained'
                dest_stage = workspace / 'foreign-stage'
                def check():
                    self.assertEqual([], list((root / 'sub').iterdir()))
                    self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())
                    if timing == 'before':
                        self.assertEqual(OWNED, dest_owned.read_bytes())
                        self.assertEqual(FOREIGN, dest_old.read_bytes())
                        self.assertFalse(target.exists())
                        self.assertFalse((retained / 'file.json').exists())
                    elif timing == 'after':
                        self.assertEqual(OWNED, dest_old.read_bytes())
                        self.assertEqual(FOREIGN, dest_stage.read_bytes())
                        self.assertFalse(target.exists())
                        self.assertFalse((retained / 'file.json').exists())
                    else:
                        self.assertEqual(OWNED, dest_old.read_bytes())
                        self.assertEqual(FOREIGN, (retained / 'file.json').read_bytes())
            assert_book_journal_fenced(self, root, content_check=check)

    def _directory_case(self, timing):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            chapters = root / 'chapters'
            chapters.mkdir()
            (chapters / 'owned').write_bytes(OWNED)
            retained = root / 'retained-workspace'
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False}
            def hooked(source, destination, **kwargs):
                pair = (str(source), str(destination))
                calls.append(pair)
                if pair == ('chapters', 'old-0') and not state['relocated']:
                    result = real_move(source, destination, **kwargs)
                    workspace = next(root.glob('.book-switch-*'))
                    workspace.rename(retained)
                    workspace.mkdir(mode=0o700)
                    state['workspace'] = workspace
                    if timing == 'before':
                        (retained / 'old-0').rename(retained / 'owned-retained')
                        (retained / 'old-0').write_bytes(FOREIGN)
                    else:
                        (retained / 'foreign-stage').write_bytes(FOREIGN)
                    state['relocated'] = True
                    return result
                if pair == ('old-0', 'chapters') and timing == 'after':
                    (retained / 'old-0').rename(retained / 'owned-retained')
                    (retained / 'foreign-stage').rename(retained / 'old-0')
                return real_move(source, destination, **kwargs)
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {}, ['chapters'])
            self.assertTrue(state['relocated'])
            self.assertIn(('chapters', 'old-0'), calls)
            self.assertNotIn(('old-0', 'chapters'), calls)
            def check():
                self.assertEqual([], list(state['workspace'].iterdir()))
                self.assertFalse((root / 'chapters').exists())
                if timing == 'before':
                    self.assertEqual(OWNED, (retained / 'owned-retained/owned').read_bytes())
                    self.assertEqual(FOREIGN, (retained / 'old-0').read_bytes())
                else:
                    self.assertEqual(OWNED, (retained / 'old-0/owned').read_bytes())
                    self.assertEqual(FOREIGN, (retained / 'foreign-stage').read_bytes())
            assert_book_journal_fenced(self, root, content_check=check)


class BookStateTransaction1660RecoveryCauseTests(unittest.TestCase):
    """Recovery failure stays primary, original trigger is explicit cause.

    Runs on all platforms: it needs no parent relocation, only a real
    apply transaction inside an already-admitted ensure_book_state.
    """

    def test_recovery_failure_explicitly_chains_original_byte_and_stream(self):
        for streaming, recovery_type in ((False, ValueError), (True, OSError)):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / 'artifact'
                target.write_bytes(OWNED)
                name = '_validate_moved_stream_original' if streaming else '_validate_moved_byte_original'
                real, called = getattr(books, name), []
                def fail_original(*args, **kwargs):
                    if not called:
                        called.append(True)
                        raise ValueError('fixture original failure')
                    return real(*args, **kwargs)
                def fail_recovery(*args, **kwargs):
                    raise recovery_type('fixture recovery failure')
                value = _stream(GENERATED) if streaming else GENERATED
                options = {'stream_byte_limit': 100} if streaming else {}
                with books.ensure_book_state(root):
                    with patch.object(books, name, side_effect=fail_original), \
                         patch.object(books, 'recover_book_state_locked', side_effect=fail_recovery):
                        with self.assertRaises(recovery_type) as raised:
                            books.apply_book_state_locked(root, {'artifact': value}, [], **options)
                self.assertEqual([True], called)
                self.assertIn('fixture recovery failure', str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, ValueError)
                self.assertIn('fixture original failure', str(raised.exception.__cause__))
                # _validate_moved_*_original fails after target->old-0 but
                # before new-0->target publishes, so target is absent and
                # both owned and generated bytes are retained in workspace.
                self.assertFalse(target.exists())
                journal = root / books.JOURNAL
                self.assertTrue(journal.exists())
                with journal.open('r', encoding='utf-8') as handle:
                    import json
                    self.assertEqual('publishing', json.load(handle)['phase'])
                workspace = next(root.glob('.book-switch-*'))
                self.assertEqual(OWNED, (workspace / 'old-0').read_bytes())
                self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())
