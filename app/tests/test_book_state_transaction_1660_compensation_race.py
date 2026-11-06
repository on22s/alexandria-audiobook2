import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books

OWNED = b'owned original'
GENERATED = b'generated'
FOREIGN = b'foreign replacement'


class BookStateTransaction1660CompensationRaceTests(unittest.TestCase):
    """Permanent regression for the #1660 residual compensation race.

    On post-move source- or destination-parent authority loss, _move must
    retain files and the journal and raise, never reverse a name that may
    now be foreign. Every case asserts the primary move and parent change
    really happened, that no reverse move was attempted, exact preserved
    names/bytes, an unchanged substitute directory, journal phase, and
    repeated recovery refusal -- a disappearing hook alone cannot pass.
    """

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
    def test_destination_parent_swap_before_and_after_byte_and_stream(self):
        import source_upload_publication as publication
        for streaming in (False, True):
            for timing in ('before', 'after'):
                with self.subTest(streaming=streaming, timing=timing):
                    self._parent_case(publication, 'destination', streaming, timing)

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
    def test_source_parent_swap_before_and_after_byte_and_stream(self):
        import source_upload_publication as publication
        for streaming in (False, True):
            for timing in ('before', 'after'):
                with self.subTest(streaming=streaming, timing=timing):
                    self._parent_case(publication, 'source', streaming, timing)

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
    def test_occupied_source_is_preserved_byte_and_stream(self):
        import source_upload_publication as publication
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self._parent_case(publication, 'destination', streaming, 'occupied')

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
    def test_direct_directory_move_preserves_on_destination_parent_change(self):
        import source_upload_publication as publication
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                self._directory_case(publication, timing)

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored _move required')
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
                value = (books.BookStateStream(io.BytesIO(GENERATED), len(GENERATED),
                         hashlib.sha256(GENERATED).hexdigest()) if streaming else GENERATED)
                options = {'stream_byte_limit': 100} if streaming else {}
                with patch.object(books, name, side_effect=fail_once):
                    with books.ensure_book_state(root), self.assertRaises(ValueError):
                        books.apply_book_state_locked(root, {'artifact': value}, [], **options)
                self.assertEqual([True], calls)
                # Valid parents: recovery restores the original and clears evidence.
                self.assertEqual(OWNED, target.read_bytes())
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertEqual([], list(root.glob('.book-switch-*')))

    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd
                         and os.mkdir in os.supports_dir_fd,
                         'POSIX descriptor-relative staging required')
    def test_root_relocation_control_asserts_admission_and_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            (root / 'script.json').write_bytes(OWNED)
            retained = base / 'retained-book'
            stage = books._stage_book_stream
            relocated = []
            def relocate_root(*args, **kwargs):
                result = stage(*args, **kwargs)
                if not relocated:
                    root.rename(retained)
                    root.mkdir()
                    (root / 'foreign').write_bytes(b'do not modify')
                    relocated.append(True)
                return result
            with patch.object(books, '_stage_book_stream', side_effect=relocate_root):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'root changed'):
                    books.apply_book_state_locked(root, {'script.json': GENERATED}, [])
            self.assertEqual([True], relocated)
            self.assertEqual(b'do not modify', (root / 'foreign').read_bytes())
            self.assertFalse((root / 'script.json').exists())
            self.assertEqual(OWNED, (retained / 'script.json').read_bytes())
            self.assertFalse((retained / books.JOURNAL).exists())
            self.assertEqual([], list(retained.glob('.book-switch-*')))

    def _assert_fenced(self, root):
        journal = root / books.JOURNAL
        self.assertTrue(journal.exists(), 'journal evidence must be preserved')
        with journal.open('r', encoding='utf-8') as handle:
            self.assertEqual('publishing', json.load(handle)['phase'])
        frozen = journal.read_bytes()
        for _ in range(2):
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Relocated recovery workspace was admitted')
            self.assertEqual(frozen, journal.read_bytes())
        return frozen

    def _parent_case(self, publication, parent, streaming, timing):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            if parent == 'destination':
                target, target_name = root / 'artifact', 'artifact'
                retained = root / 'retained-workspace'
                primary, reverse = ('artifact', 'old-0'), ('old-0', 'artifact')
            else:
                sub = root / 'sub'
                sub.mkdir()
                target, target_name = sub / 'file.json', 'file.json'
                retained = root / 'retained-sub'
                primary, reverse = ('file.json', 'old-0'), ('old-0', 'file.json')
            target.write_bytes(OWNED)
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False, 'reverse_attempted': False}
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
                if pair == reverse:
                    state['reverse_attempted'] = True
                    if timing == 'after':
                        # Attempts the post-observation swap before the real
                        # rename; fail-closed code must never reach here.
                        base = retained if parent == 'destination' else state['workspace']
                        (base / 'old-0').rename(base / 'owned-retained')
                        (base / 'foreign-stage').rename(base / 'old-0')
                    return real_move(source, destination, **kwargs)
                return real_move(source, destination, **kwargs)
            value = (books.BookStateStream(io.BytesIO(GENERATED), len(GENERATED),
                     hashlib.sha256(GENERATED).hexdigest()) if streaming else GENERATED)
            options = {'stream_byte_limit': 100} if streaming else {}
            replacements = {'artifact': value} if parent == 'destination' else {'sub/file.json': value}
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, replacements, [], **options)
            # Actual intervention, not a disappearing hook.
            self.assertTrue(state['relocated'])
            self.assertIn(primary, calls)
            self.assertFalse(state['reverse_attempted'])
            self.assertNotIn(reverse, calls)
            workspace = state['workspace']
            self.assertTrue(workspace.is_dir())
            if parent == 'destination':
                self.assertEqual([], list(workspace.iterdir()), 'substitute workspace must stay unchanged')
                self.assertEqual(GENERATED, (retained / 'new-0').read_bytes())
                dest_old, dest_owned = retained / 'old-0', retained / 'owned-retained'
                dest_stage = retained / 'foreign-stage'
            else:
                self.assertEqual([], list((root / 'sub').iterdir()), 'substitute source parent must stay unchanged')
                self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())
                self.assertFalse((retained / 'file.json').exists() and timing != 'occupied')
                dest_old, dest_owned = workspace / 'old-0', workspace / 'owned-retained'
                dest_stage = workspace / 'foreign-stage'
            if timing == 'before':
                self.assertEqual(OWNED, dest_owned.read_bytes())
                self.assertEqual(FOREIGN, dest_old.read_bytes())
                self.assertFalse(target.exists())
            elif timing == 'after':
                self.assertEqual(OWNED, dest_old.read_bytes())
                self.assertEqual(FOREIGN, dest_stage.read_bytes())
                self.assertFalse(target.exists())
            else:  # occupied
                self.assertEqual(OWNED, dest_old.read_bytes())
                occupied = (root / 'artifact') if parent == 'destination' else (retained / 'file.json')
                self.assertEqual(FOREIGN, occupied.read_bytes(), 'occupied source must be preserved, not overwritten')
            frozen = self._assert_fenced(root)
            # Repeated refusal must not move anything.
            if timing == 'before':
                self.assertEqual(FOREIGN, dest_old.read_bytes())
                self.assertEqual(OWNED, dest_owned.read_bytes())
            elif timing == 'after':
                self.assertEqual(OWNED, dest_old.read_bytes())
                self.assertEqual(FOREIGN, dest_stage.read_bytes())
            self.assertEqual(frozen, (root / books.JOURNAL).read_bytes())

    def _directory_case(self, publication, timing):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            chapters = root / 'chapters'
            chapters.mkdir()
            (chapters / 'owned').write_bytes(OWNED)
            retained = root / 'retained-workspace'
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False, 'reverse_attempted': False}
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
                if pair == ('old-0', 'chapters'):
                    state['reverse_attempted'] = True
                    if timing == 'after':
                        (retained / 'old-0').rename(retained / 'owned-retained')
                        (retained / 'foreign-stage').rename(retained / 'old-0')
                    return real_move(source, destination, **kwargs)
                return real_move(source, destination, **kwargs)
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {}, ['chapters'])
            self.assertTrue(state['relocated'])
            self.assertIn(('chapters', 'old-0'), calls)
            self.assertFalse(state['reverse_attempted'])
            self.assertNotIn(('old-0', 'chapters'), calls)
            self.assertEqual([], list(state['workspace'].iterdir()))
            self.assertFalse((root / 'chapters').exists())
            if timing == 'before':
                self.assertEqual(OWNED, (retained / 'owned-retained/owned').read_bytes())
                self.assertEqual(FOREIGN, (retained / 'old-0').read_bytes())
            else:
                self.assertEqual(OWNED, (retained / 'old-0/owned').read_bytes())
                self.assertEqual(FOREIGN, (retained / 'foreign-stage').read_bytes())
            self._assert_fenced(root)
