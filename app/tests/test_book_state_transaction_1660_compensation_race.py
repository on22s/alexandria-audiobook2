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
from tests.test_support import (MOVE_PARENT_CHANGED_MESSAGE, ORIGINAL_MISSING_MESSAGE,
                                PARENT_CHANGED_MESSAGE, ROOT_ADMISSION_MESSAGE,
                                ROOT_CHANGED_MESSAGE, assert_book_journal_fenced)

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

    def test_source_parent_occupied_source_is_preserved_byte_and_stream(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self._parent_case('source', streaming, 'occupied')

    def test_nested_direct_directory_move_preserves_on_source_parent_change(self):
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                self._nested_directory_case(timing)

    def test_ancestor_chain_swap_after_parent_check_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / 'a' / 'b'
            parent.mkdir(parents=True)
            target = parent / 'file'
            target.write_bytes(OWNED)
            retained_a = root / 'retained-a'
            validate = books._validate_staged_replacement
            staged_calls, swapped = [], []
            def substitute(*args, **kwargs):
                result = validate(*args, **kwargs)
                if len(args) > 1 and str(args[1]).endswith('new-0'):
                    staged_calls.append(True)
                    if len(staged_calls) == 2:
                        (root / 'a').rename(retained_a)
                        (root / 'a').mkdir(mode=0o700)
                        (retained_a / 'b').rename(root / 'a/b')
                        swapped.append(True)
                return result
            with patch.object(books, '_validate_staged_replacement', side_effect=substitute):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'parent changed'):
                    books.apply_book_state_locked(root, {'a/b/file': GENERATED}, [])
            self.assertEqual([True], swapped)
            self.assertEqual(OWNED, (root / 'a/b/file').read_bytes())
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())

    def test_ancestor_chain_swap_after_primary_move_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / 'a' / 'b'
            parent.mkdir(parents=True)
            workspace = root / 'workspace'
            workspace.mkdir()
            source = parent / 'artifact'
            destination = workspace / 'old-0'
            source.write_bytes(OWNED)
            source_version = books._book_target_version(source)
            identity = [source_version[index] for index in (0, 1, 2, 3, 5, 6)]
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(root, flags)
            try:
                source_chain = books._capture_target_parent_identities(root_fd, 'a/b/artifact')
                destination_chain = books._capture_target_parent_identities(root_fd, 'workspace/old-0')
            finally:
                os.close(root_fd)
            retained = root / 'retained-a'
            move = publication.move_source_upload_exclusively
            swapped = []
            def swap_after_move(*args, **kwargs):
                result = move(*args, **kwargs)
                (root / 'a').rename(retained)
                (root / 'a').mkdir(mode=0o700)
                (retained / 'b').rename(root / 'a/b')
                swapped.append(True)
                return result
            with patch.object(publication, 'move_source_upload_exclusively',
                              side_effect=swap_after_move):
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._move(
                        source, destination, exclusive=True, anchor=root,
                        source_parent_identity=source_chain,
                        destination_parent_identity=destination_chain,
                        expected_source_identity=identity)
            self.assertEqual([True], swapped)
            self.assertFalse(source.exists())
            self.assertEqual(OWNED, destination.read_bytes())

    def test_missing_workspace_recreated_after_admission_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            workspace = root / '.book-switch-test'
            target_version = books._book_target_version(target)
            document = {'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                        'entries': [{'path': 'artifact', 'existed': False,
                                     'old_sha256': None,
                                     'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                                     'new_file_identity': [target_version[0], target_version[1]]}]}
            books._save_journal(root, document)
            validate = books._validate_byte_replacement
            recreated = []
            def recreate_after_validation(*args, **kwargs):
                result = validate(*args, **kwargs)
                if not recreated:
                    workspace.mkdir()
                    (workspace / 'foreign').write_bytes(FOREIGN)
                    recreated.append(True)
                return result
            with patch.object(books, '_validate_byte_replacement', side_effect=recreate_after_validation):
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books.recover_book_state_locked(root)
            self.assertEqual([True], recreated)
            self.assertEqual(GENERATED, target.read_bytes())
            self.assertEqual(FOREIGN, (workspace / 'foreign').read_bytes())
            with (root / books.JOURNAL).open('r', encoding='utf-8') as handle:
                import json
                self.assertEqual('publishing', json.load(handle)['phase'])

    def test_persisted_workspace_identity_refuses_preexisting_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            target_version = books._book_target_version(target)
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            workspace_info = workspace.lstat()
            document = {
                'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                'workspace_identity': [workspace_info.st_dev, workspace_info.st_ino],
                'entries': [{'path': 'artifact', 'existed': False,
                             'old_sha256': None,
                             'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                             'new_file_identity': [target_version[0], target_version[1]]}],
            }
            books._save_journal(root, document)
            retained = root / 'retained-workspace'
            workspace.rename(retained)
            workspace.mkdir()
            (workspace / 'foreign').write_bytes(FOREIGN)
            with self.assertRaisesRegex(ValueError, 'workspace changed'):
                books.recover_book_state_locked(root)
            self.assertEqual(GENERATED, target.read_bytes())
            self.assertEqual(FOREIGN, (workspace / 'foreign').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_removal_recovery_refuses_target_created_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            original = workspace / 'old-0'
            original.write_bytes(OWNED)
            old_version = books._book_target_version(original)
            workspace_info = workspace.lstat()
            document = {
                'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                'workspace_identity': [workspace_info.st_dev, workspace_info.st_ino],
                'entries': [{'path': 'artifact', 'existed': True,
                             'old_sha256': hashlib.sha256(OWNED).hexdigest(),
                             'new_sha256': None,
                             'old_file_identity': [old_version[i] for i in (0, 1, 2, 3, 5, 6)]}],
            }
            books._save_journal(root, document)
            lexists = os.path.lexists
            observations = []
            def create_after_absent_observation(path):
                present = lexists(path)
                if Path(path) == target and not observations:
                    observations.append(present)
                    self.assertFalse(present)
                    target.write_bytes(FOREIGN)
                return present
            with patch.object(books.os.path, 'lexists', side_effect=create_after_absent_observation):
                with self.assertRaisesRegex(ValueError, 'removal target is occupied'):
                    books.recover_book_state_locked(root)
            self.assertEqual([False], observations)
            self.assertEqual(FOREIGN, target.read_bytes())
            self.assertEqual(OWNED, original.read_bytes())
            self.assertFalse((workspace / 'discard-0').exists())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_generation_oserror_is_normalized_before_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            version = books._book_target_version(source)
            identity = [version[index] for index in (0, 1, 2, 3, 5, 6)]
            with patch.object(books, 'get_book_file_generation',
                              side_effect=OSError('fixture generation failure')):
                with self.assertRaises(ValueError) as raised:
                    books._move(source, root / 'old-0', exclusive=True, anchor=root,
                                expected_source_identity=identity, expected_source_generation=1)
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(OWNED, source.read_bytes())
            self.assertFalse((root / 'old-0').exists())

    def test_post_restore_missing_target_is_normalized_and_fenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            original = workspace / 'old-0'
            original.write_bytes(OWNED)
            old_version = books._book_target_version(original)
            new_version = books._book_target_version(target)
            document = {'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                        'entries': [{'path': 'artifact', 'existed': True,
                                     'old_sha256': hashlib.sha256(OWNED).hexdigest(),
                                     'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                                     'old_file_identity': [old_version[i] for i in (0, 1, 2, 3, 5, 6)],
                                     'new_file_identity': [new_version[0], new_version[1]]}]}
            books._save_journal(root, document)
            real_move = publication.move_source_upload_exclusively
            retained = workspace / 'owned-retained'
            moved = []
            def remove_named_target(source, destination, **kwargs):
                result = real_move(source, destination, **kwargs)
                if str(source) == 'old-0' and str(destination) == 'artifact' and not moved:
                    target.rename(retained)
                    moved.append(True)
                return result
            with patch.object(publication, 'move_source_upload_exclusively', remove_named_target):
                with self.assertRaises(ValueError) as raised:
                    books.recover_book_state_locked(root)
            self.assertEqual([True], moved)
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
            self.assertEqual(OWNED, retained.read_bytes())
            with (root / books.JOURNAL).open('r', encoding='utf-8') as handle:
                import json
                self.assertEqual('publishing', json.load(handle)['phase'])

    def test_stream_post_restore_lstat_error_is_normalized_and_fenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            target_version = books._book_target_version(target)
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            workspace_info = workspace.lstat()
            document = {
                'version': 2, 'phase': 'publishing', 'workspace': workspace.name,
                'workspace_identity': [workspace_info.st_dev, workspace_info.st_ino],
                'stream_limits': {'bytes': 1000, 'entries': 10},
                'entries': [{'path': 'artifact', 'existed': False,
                             'old_sha256': None,
                             'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                             'new_file_identity': [target_version[0], target_version[1]]}],
            }
            books._save_journal(root, document)
            real_lstat = Path.lstat
            observations = []
            def fail_absent_target(path, *args, **kwargs):
                if path == target and not os.path.lexists(path):
                    observations.append(True)
                    raise PermissionError('fixture lstat refusal')
                return real_lstat(path, *args, **kwargs)
            with patch.object(Path, 'lstat', fail_absent_target):
                with self.assertRaisesRegex(ValueError, 'Recovered book target changed') as raised:
                    books.recover_book_state_locked(root)
            self.assertEqual([True], observations)
            self.assertIsInstance(raised.exception.__cause__, PermissionError)
            self.assertEqual(GENERATED, (workspace / 'discard-0').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_direct_move_expectation_requires_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            with self.assertRaisesRegex(ValueError, 'expectation'):
                books._move(source, root / 'old-0', exclusive=True,
                            expected_source_identity=[1, 2])
            with self.assertRaisesRegex(ValueError, 'expectation'):
                books._move(source, root / 'old-0', exclusive=False, anchor=root,
                            expected_source_identity=[1, 2])
            self.assertEqual(OWNED, source.read_bytes())

    def test_expected_recovery_root_refuses_different_resolved_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            admitted = base / 'admitted'
            foreign = base / 'foreign'
            admitted.mkdir(); foreign.mkdir()
            (foreign / 'marker').write_bytes(FOREIGN)
            admitted_identity = (admitted.stat().st_dev, admitted.stat().st_ino)
            foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
            with patch.object(books, '_direct_book_root', return_value=(foreign, foreign_identity)):
                with self.assertRaisesRegex(ValueError, 'Book root changed during admission'):
                    books.recover_book_state_locked(
                        'alias-spelling', expected_root=admitted,
                        expected_root_identity=admitted_identity)
            self.assertEqual(FOREIGN, (foreign / 'marker').read_bytes())

    def test_root_relocation_after_primary_move_byte_and_stream(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                root = base / 'book'
                root.mkdir()
                target = root / 'artifact'
                target.write_bytes(OWNED)
                retained_root = base / 'retained-book'
                real_move = publication.move_source_upload_exclusively
                calls, state = [], {'relocated': False}
                def hooked(source, destination, **kwargs):
                    pair = (str(source), str(destination))
                    calls.append(pair)
                    result = real_move(source, destination, **kwargs)
                    if pair == ('artifact', 'old-0') and not state['relocated']:
                        root.rename(retained_root)
                        root.mkdir(mode=0o700)
                        (root / 'foreign').write_bytes(FOREIGN)
                        state['relocated'] = True
                    return result
                value = _stream(GENERATED) if streaming else GENERATED
                options = {'stream_byte_limit': 100} if streaming else {}
                with patch.object(publication, 'move_source_upload_exclusively', hooked):
                    with books.ensure_book_state(root), self.assertRaises(ValueError) as raised:
                        books.apply_book_state_locked(root, {'artifact': value}, [], **options)
                self.assertEqual(ROOT_ADMISSION_MESSAGE, str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, ValueError)
                self.assertEqual(ROOT_CHANGED_MESSAGE, str(raised.exception.__cause__))
                self.assertTrue(state['relocated'])
                self.assertIn(('artifact', 'old-0'), calls)
                self.assertNotIn(('old-0', 'artifact'), calls)
                self.assertEqual(FOREIGN, (root / 'foreign').read_bytes())
                self.assertFalse((retained_root / 'artifact').exists())
                workspace = next(retained_root.glob('.book-switch-*'))
                self.assertEqual(OWNED, (workspace / 'old-0').read_bytes())
                self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())
                journal = retained_root / books.JOURNAL
                self.assertTrue(journal.exists())
                with journal.open('r', encoding='utf-8') as handle:
                    import json
                    self.assertEqual('publishing', json.load(handle)['phase'])

    def test_root_disappearance_after_primary_move_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            target = root / 'artifact'
            target.write_bytes(OWNED)
            retained_root = base / 'retained-book'
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False}
            def hooked(source, destination, **kwargs):
                pair = (str(source), str(destination))
                calls.append(pair)
                result = real_move(source, destination, **kwargs)
                if pair == ('artifact', 'old-0') and not state['relocated']:
                    root.rename(retained_root)
                    state['relocated'] = True
                return result
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises(FileNotFoundError) as raised:
                    books.apply_book_state_locked(root, {'artifact': GENERATED}, [])
            self.assertTrue(state['relocated'])
            self.assertIn(('artifact', 'old-0'), calls)
            self.assertNotIn(('old-0', 'artifact'), calls)
            self.assertFalse(root.exists())
            workspace = next(retained_root.glob('.book-switch-*'))
            self.assertEqual(OWNED, (workspace / 'old-0').read_bytes())
            self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())
            journal = retained_root / books.JOURNAL
            self.assertTrue(journal.exists())
            with journal.open('r', encoding='utf-8') as handle:
                import json
                self.assertEqual('publishing', json.load(handle)['phase'])
            self.assertIsInstance(raised.exception.__cause__, ValueError)
            self.assertEqual(ROOT_CHANGED_MESSAGE, str(raised.exception.__cause__))
            self.assertIsInstance(raised.exception.__cause__.__cause__, FileNotFoundError)

    def test_recovery_source_substitution_is_fenced_byte_and_stream(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / 'artifact'
                target.write_bytes(GENERATED)
                workspace = root / '.book-switch-test'
                workspace.mkdir()
                (workspace / 'old-0').write_bytes(OWNED)
                old_version = books._book_target_version(workspace / 'old-0')
                new_version = books._book_target_version(target)
                budget = {'bytes': 100000, 'entries': 10}
                document = {'version': 2 if streaming else 3, 'phase': 'publishing', 'workspace': workspace.name,
                            'entries': [{'path': 'artifact', 'existed': True,
                                         'old_sha256': hashlib.sha256(OWNED).hexdigest(),
                                         'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                                         'old_file_identity': [old_version[i] for i in (0, 1, 2, 3, 5, 6)],
                                         'new_file_identity': [new_version[0], new_version[1]]}]}
                if streaming:
                    document['stream_limits'] = budget
                books._save_journal(root, document)
                validate = books._validate_stream_original if streaming else books._validate_byte_original
                swapped = []
                def swap_after_validate(*args, **kwargs):
                    result = validate(*args, **kwargs)
                    if not swapped:
                        (workspace / 'old-0').rename(workspace / 'owned-retained')
                        (workspace / 'old-0').write_bytes(FOREIGN)
                        swapped.append(True)
                    return result
                name = '_validate_stream_original' if streaming else '_validate_byte_original'
                with patch.object(books, name, side_effect=swap_after_validate):
                    with self.assertRaises(ValueError):
                        books.recover_book_state_locked(root)
                self.assertEqual([True], swapped)
                self.assertFalse(target.exists() and target.read_bytes() == FOREIGN)
                self.assertEqual(FOREIGN, (workspace / 'old-0').read_bytes())
                self.assertEqual(OWNED, (workspace / 'owned-retained').read_bytes())
                journal = root / books.JOURNAL
                with journal.open('r', encoding='utf-8') as handle:
                    import json
                    self.assertEqual('publishing', json.load(handle)['phase'])

    def test_persisted_nested_parent_substitution_before_move_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sub = root / 'sub'
            sub.mkdir()
            target = sub / 'file.json'
            target.write_bytes(OWNED)
            retained_sub = root / 'retained-sub'
            validate = books._validate_staged_replacement
            swapped, staged_calls = [], []
            def substitute_after_check(*args, **kwargs):
                result = validate(*args, **kwargs)
                if len(args) > 1 and str(args[1]).endswith('new-0'):
                    staged_calls.append(True)
                    if len(staged_calls) == 2:
                        sub.rename(retained_sub)
                        sub.mkdir(mode=0o700)
                        swapped.append(True)
                return result
            with patch.object(books, '_validate_staged_replacement', side_effect=substitute_after_check):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'sub/file.json': GENERATED}, [])
            self.assertEqual([True], swapped)
            self.assertFalse((sub / 'file.json').exists())
            self.assertEqual(OWNED, (retained_sub / 'file.json').read_bytes())
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(GENERATED, (workspace / 'new-0').read_bytes())

    def test_anchor_identity_mismatch_both_replacement_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            (root / 'artifact').write_bytes(OWNED)
            retained = base / 'retained-book'
            old_identity = (root.stat().st_dev, root.stat().st_ino)
            root.rename(retained)
            root.mkdir(mode=0o700)
            (root / 'artifact').write_bytes(FOREIGN)
            workspace = root / 'workspace'
            workspace.mkdir()
            with self.assertRaisesRegex(ValueError, 'Book move root changed'):
                books._move(root / 'artifact', workspace / 'old-0', exclusive=True, anchor=root,
                            anchor_identity=old_identity)
            self.assertEqual(FOREIGN, (root / 'artifact').read_bytes())
            self.assertFalse((workspace / 'old-0').exists())
            self.assertEqual(OWNED, (retained / 'artifact').read_bytes())

    def test_recovery_root_change_during_reverse_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            (workspace / 'old-0').write_bytes(OWNED)
            old_version = books._book_target_version(workspace / 'old-0')
            new_version = books._book_target_version(target)
            document = {'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                        'entries': [{'path': 'artifact', 'existed': True,
                                     'old_sha256': hashlib.sha256(OWNED).hexdigest(),
                                     'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                                     'old_file_identity': [old_version[i] for i in (0, 1, 2, 3, 5, 6)],
                                     'new_file_identity': [new_version[0], new_version[1]]}]}
            books._save_journal(root, document)
            retained_root = base / 'retained-book'
            real_move = publication.move_source_upload_exclusively
            moved = []
            def relocate_after_first(source, destination, **kwargs):
                result = real_move(source, destination, **kwargs)
                if str(source) == 'artifact' and str(destination) == 'discard-0' and not moved:
                    root.rename(retained_root)
                    root.mkdir(mode=0o700)
                    moved.append(True)
                return result
            with patch.object(publication, 'move_source_upload_exclusively', relocate_after_first):
                with self.assertRaisesRegex(ValueError, 'Book move root changed'):
                    books.recover_book_state_locked(root)
            self.assertEqual([True], moved)
            self.assertFalse((root / 'artifact').exists())
            retained_workspace = retained_root / workspace.name
            self.assertEqual(GENERATED, (retained_workspace / 'discard-0').read_bytes())
            self.assertEqual(OWNED, (retained_workspace / 'old-0').read_bytes())
            journal = retained_root / books.JOURNAL
            with journal.open('r', encoding='utf-8') as handle:
                import json
                self.assertEqual('publishing', json.load(handle)['phase'])

    def test_alias_spelling_recovery_does_not_bypass_admission(self):
        import direct_path_admission as admission
        import stat as stat_module
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary).resolve()
            base = fixture / 'base'
            target_dir = fixture / 'target'
            base.mkdir(mode=0o755)
            target_dir.mkdir(mode=0o755)
            book = target_dir / 'book'
            book.mkdir()
            (book / 'artifact').write_bytes(OWNED)
            alias = base / 'alias'
            alias.symlink_to(target_dir, target_is_directory=True)
            data_dir = alias / 'book'
            real_stat = admission._stat_directory
            def stat_directory(path):
                if path == fixture or fixture in path.parents:
                    return real_stat(path)
                return type('Info', (), {'st_mode': stat_module.S_IFDIR | 0o755, 'st_uid': 0})()
            owners = frozenset({0, os.getuid()})
            with patch.object(admission, 'TRUSTED_ALIAS_OWNERS', owners), \
                 patch.object(admission, '_stat_directory', side_effect=stat_directory):
                with books.ensure_book_state(data_dir):
                    rebound = []
                    real_move = publication.move_source_upload_exclusively
                    def rebound_after_move(source, destination, **kwargs):
                        result = real_move(source, destination, **kwargs)
                        if str(source) == 'artifact' and str(destination) == 'old-0' and not rebound:
                            target_dir.rename(fixture / 'retained-target')
                            target_dir.mkdir(mode=0o755)
                            (target_dir / 'book').mkdir(mode=0o755)
                            (target_dir / 'book' / 'foreign').write_bytes(FOREIGN)
                            rebound.append(True)
                        return result
                    with patch.object(publication, 'move_source_upload_exclusively', rebound_after_move):
                        with self.assertRaisesRegex(ValueError, 'Book root changed during admission'):
                            books.apply_book_state_locked(data_dir, {'artifact': GENERATED}, [])
                    self.assertEqual([True], rebound)
                    self.assertEqual(FOREIGN, (target_dir / 'book' / 'foreign').read_bytes())

    def test_recovery_post_move_source_substitution_is_fenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            target.write_bytes(GENERATED)
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            (workspace / 'old-0').write_bytes(OWNED)
            old_version = books._book_target_version(workspace / 'old-0')
            new_version = books._book_target_version(target)
            document = {'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                        'entries': [{'path': 'artifact', 'existed': True,
                                     'old_sha256': hashlib.sha256(OWNED).hexdigest(),
                                     'new_sha256': hashlib.sha256(GENERATED).hexdigest(),
                                     'old_file_identity': [old_version[i] for i in (0, 1, 2, 3, 5, 6)],
                                     'new_file_identity': [new_version[0], new_version[1]]}]}
            books._save_journal(root, document)
            real_move = publication.move_source_upload_exclusively
            swapped = []
            def swap_before_rename(source, destination, **kwargs):
                if str(source) == 'old-0' and str(destination) == 'artifact' and not swapped:
                    (workspace / 'old-0').rename(workspace / 'owned-retained')
                    (workspace / 'old-0').write_bytes(FOREIGN)
                    swapped.append(True)
                return real_move(source, destination, **kwargs)
            with patch.object(publication, 'move_source_upload_exclusively', swap_before_rename):
                with self.assertRaises(ValueError):
                    books.recover_book_state_locked(root)
            self.assertEqual([True], swapped)
            self.assertEqual(FOREIGN, target.read_bytes())
            self.assertEqual(OWNED, (workspace / 'owned-retained').read_bytes())
            journal = root / books.JOURNAL
            with journal.open('r', encoding='utf-8') as handle:
                import json
                self.assertEqual('publishing', json.load(handle)['phase'])

    def test_initial_root_disappearance_is_stable_value_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            retained = base / 'retained-book'
            real_fstat = os.fstat
            moved = []
            def rename_on_first_fstat(fd):
                result = real_fstat(fd)
                if not moved:
                    root.rename(retained)
                    moved.append(True)
                return result
            with patch.object(books.os, 'fstat', rename_on_first_fstat):
                with self.assertRaises(ValueError) as raised:
                    books._move(source, root / 'old-0', exclusive=True, anchor=root)
            self.assertEqual([True], moved)
            self.assertEqual(ROOT_CHANGED_MESSAGE, str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
            self.assertEqual(OWNED, (retained / 'artifact').read_bytes())
            self.assertFalse((retained / 'old-0').exists())

    def test_nested_parent_open_failure_is_stable_value_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'file').write_bytes(OWNED)
            sub = root / 'sub'
            sub.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError) as raised:
                books._move(root / 'sub/file', root / 'old-0', exclusive=True, anchor=root)
            self.assertEqual(MOVE_PARENT_CHANGED_MESSAGE, str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(OWNED, (outside / 'file').read_bytes())
            self.assertFalse((root / 'old-0').exists())

    def test_direct_directory_move_preserves_on_destination_parent_change(self):
        # Bounded duplication is intentional here: a directory removal has
        # no generated stage and different postconditions (contents, not
        # bytes), so sharing the file harness would hide those oracles.
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                self._directory_case(timing)

    def _run_sync_move(self, sync_failures):
        """Real POSIX _move + relocation; sync_failures maps call index to raise."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            retained = root / 'retained'
            real_move, real_fsync = publication.move_source_upload_exclusively, os.fsync
            state = {'moved': False}
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
                    index = len(sync_calls)
                    sync_calls.append((fd, os.fstat(fd).st_ino))
                    if index in sync_failures:
                        raise sync_failures[index]
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
            self.assertTrue(sync_calls)
            self.assertEqual(OWNED, (retained / 'old-0').read_bytes())
            self.assertFalse(source.exists())
            self.assertEqual([], list(workspace.iterdir()))
            return raised, sync_calls, root.stat().st_ino, retained.stat().st_ino

    def test_sync_oserror_preserves_parent_error_as_primary_with_sync_cause(self):
        raised, sync_calls, root_ino, retained_ino = self._run_sync_move(
            {0: OSError(errno.EIO, 'fixture directory sync failure')})
        self.assertIsInstance(raised, ValueError)
        self.assertIn('parent changed', str(raised))
        self.assertIsInstance(raised.__cause__, OSError)
        self.assertIn('fixture directory sync failure', str(raised.__cause__))
        # Both distinct parent fds attempted, source (root) first.
        self.assertEqual(2, len(sync_calls))
        self.assertEqual(root_ino, sync_calls[0][1])
        self.assertEqual(retained_ino, sync_calls[1][1])
        self.assertNotEqual(sync_calls[0][0], sync_calls[1][0])

    def test_sync_error_retains_original_root_disappearance_cause(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / 'book'
            root.mkdir()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            retained = base / 'retained'
            real_move, real_fsync = publication.move_source_upload_exclusively, os.fsync
            state = {'moved': False, 'sync_failed': False}
            sync_detail = RuntimeError('fixture pre-existing sync detail')
            sync_error = OSError(errno.EIO, 'fixture directory sync failure')
            sync_error.__cause__ = sync_detail
            def move_then_remove_root(source_name, dest_name, **kwargs):
                result = real_move(source_name, dest_name, **kwargs)
                root.rename(retained)
                state['moved'] = True
                return result
            def fail_first_sync(fd):
                if state['moved'] and not state['sync_failed']:
                    state['sync_failed'] = True
                    raise sync_error
                return real_fsync(fd)
            with patch.object(publication, 'move_source_upload_exclusively', move_then_remove_root), \
                 patch.object(books.os, 'fsync', fail_first_sync):
                with self.assertRaises(ValueError) as raised:
                    books._move(source, root / 'old-0', exclusive=True, anchor=root)
            self.assertTrue(state['sync_failed'])
            self.assertIn('root changed', str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn('fixture directory sync failure', str(raised.exception.__cause__))
            self.assertIs(sync_detail, raised.exception.__cause__.__cause__)
            self.assertIsInstance(sync_detail.__cause__, FileNotFoundError)
            self.assertEqual(OWNED, (retained / 'old-0').read_bytes())

    def test_sync_error_without_original_cause_has_no_context_cycle(self):
        raised, _sync_calls, _root_ino, _retained_ino = self._run_sync_move(
            {0: OSError(errno.EIO, 'fixture directory sync failure')})
        self.assertIsInstance(raised.__cause__, OSError)
        self.assertIsNone(raised.__cause__.__context__)

    def test_sync_second_oserror_becomes_explicit_cause(self):
        raised, sync_calls, root_ino, retained_ino = self._run_sync_move(
            {1: OSError(errno.EIO, 'fixture destination sync failure')})
        self.assertIsInstance(raised, ValueError)
        self.assertIn('parent changed', str(raised))
        self.assertIsInstance(raised.__cause__, OSError)
        self.assertIn('fixture destination sync failure', str(raised.__cause__))
        self.assertEqual(2, len(sync_calls))
        self.assertEqual(root_ino, sync_calls[0][1])
        self.assertEqual(retained_ino, sync_calls[1][1])
        self.assertNotEqual(sync_calls[0][0], sync_calls[1][0])

    def test_sync_both_oserror_first_remains_explicit_cause(self):
        raised, sync_calls, root_ino, retained_ino = self._run_sync_move(
            {0: OSError(errno.EIO, 'fixture first sync failure'),
             1: OSError(errno.EIO, 'fixture second sync failure')})
        self.assertIsInstance(raised, ValueError)
        self.assertIn('parent changed', str(raised))
        self.assertIsInstance(raised.__cause__, OSError)
        self.assertIn('fixture first sync failure', str(raised.__cause__))
        seen, chain = set(), []
        error = raised
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            chain.append(error)
            error = error.__cause__ if error.__cause__ is not None else error.__context__
        self.assertIn('fixture first sync failure', str(chain[1]))
        for entry in chain:
            self.assertNotIn('second sync failure', str(entry))
        self.assertEqual(2, len(sync_calls))
        self.assertEqual(root_ino, sync_calls[0][1])
        self.assertEqual(retained_ino, sync_calls[1][1])
        self.assertNotEqual(sync_calls[0][0], sync_calls[1][0])

    def test_successful_move_attempts_both_parent_syncs_after_first_oserror(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            source = root / 'artifact'
            destination = workspace / 'old-0'
            source.write_bytes(OWNED)
            real_move = publication.move_source_upload_exclusively
            real_fsync = os.fsync
            state = {'moved': False}
            sync_calls = []
            def move_then_mark(*args, **kwargs):
                result = real_move(*args, **kwargs)
                state['moved'] = True
                return result
            def fail_first_sync(fd):
                if state['moved']:
                    sync_calls.append((fd, os.fstat(fd).st_ino))
                    if len(sync_calls) == 1:
                        raise OSError(errno.EIO, 'fixture first parent sync failure')
                return real_fsync(fd)
            with patch.object(publication, 'move_source_upload_exclusively', side_effect=move_then_mark), \
                 patch.object(books.os, 'fsync', side_effect=fail_first_sync):
                with self.assertRaisesRegex(OSError, 'fixture first parent sync failure'):
                    books._move(source, destination, exclusive=True, anchor=root)
            self.assertEqual(2, len(sync_calls))
            self.assertEqual({root.stat().st_ino, workspace.stat().st_ino},
                             {entry[1] for entry in sync_calls})
            self.assertFalse(source.exists())
            self.assertEqual(OWNED, destination.read_bytes())

    def test_sync_keyboard_interrupt_is_not_converted_to_value_error(self):
        raised, sync_calls, root_ino, _retained_ino = self._run_sync_move({0: KeyboardInterrupt()})
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(1, len(sync_calls))
        self.assertIsNone(raised.__cause__)
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
            self.assertEqual(2, len(sync_calls))
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
            assert_book_journal_fenced(self, root, content_check=check,
                expected_message='Book transaction workspace changed; recovery evidence was preserved')

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
                with books.ensure_book_state(root), self.assertRaises(ValueError):
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
            assert_book_journal_fenced(self, root, content_check=check,
                expected_message=(PARENT_CHANGED_MESSAGE if parent == 'source'
                                 else 'Book transaction workspace changed; recovery evidence was preserved'))

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
                with books.ensure_book_state(root), self.assertRaises(ValueError):
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
            assert_book_journal_fenced(self, root, content_check=check,
                expected_message='Book transaction workspace changed; recovery evidence was preserved')

    def _nested_directory_case(self, timing):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sub = root / 'sub'
            sub.mkdir()
            chapters = sub / 'chapters'
            chapters.mkdir()
            (chapters / 'owned').write_bytes(OWNED)
            retained_sub = root / 'retained-sub'
            real_move = publication.move_source_upload_exclusively
            calls, state = [], {'relocated': False}
            def hooked(source, destination, **kwargs):
                pair = (str(source), str(destination))
                calls.append(pair)
                if pair == ('chapters', 'old-0') and not state['relocated']:
                    result = real_move(source, destination, **kwargs)
                    sub.rename(retained_sub)
                    sub.mkdir(mode=0o700)
                    workspace = next(root.glob('.book-switch-*'))
                    state['workspace'] = workspace
                    if timing == 'before':
                        (workspace / 'old-0').rename(workspace / 'owned-retained')
                        (workspace / 'old-0').write_bytes(FOREIGN)
                    else:
                        (workspace / 'foreign-stage').write_bytes(FOREIGN)
                    state['relocated'] = True
                    return result
                if pair == ('old-0', 'chapters') and timing == 'after':
                    nested_workspace = state['workspace']
                    (nested_workspace / 'old-0').rename(nested_workspace / 'owned-retained')
                    (nested_workspace / 'foreign-stage').rename(nested_workspace / 'old-0')
                return real_move(source, destination, **kwargs)
            with patch.object(publication, 'move_source_upload_exclusively', hooked):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {}, ['sub/chapters'])
            self.assertTrue(state['relocated'])
            self.assertIn(('chapters', 'old-0'), calls)
            self.assertNotIn(('old-0', 'chapters'), calls)
            workspace = state['workspace']
            def check():
                self.assertEqual([], list((root / 'sub').iterdir()))
                self.assertFalse((root / 'sub/chapters').exists())
                if timing == 'before':
                    self.assertEqual(OWNED, (workspace / 'owned-retained/owned').read_bytes())
                    self.assertEqual(FOREIGN, (workspace / 'old-0').read_bytes())
                else:
                    self.assertEqual(OWNED, (workspace / 'old-0/owned').read_bytes())
                    self.assertEqual(FOREIGN, (workspace / 'foreign-stage').read_bytes())
            assert_book_journal_fenced(self, root, content_check=check,
                expected_message=PARENT_CHANGED_MESSAGE)


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


class BookStateTransaction1660MoveExpectationTests(unittest.TestCase):
    def test_noop_removal_recovery_preserves_target_created_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'artifact'
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            workspace_info = workspace.lstat()
            document = {
                'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                'workspace_identity': [workspace_info.st_dev, workspace_info.st_ino],
                'entries': [{'path': 'artifact', 'existed': False,
                             'old_sha256': None, 'new_sha256': None}],
            }
            books._save_journal(root, document)
            lexists = os.path.lexists
            observations = []
            def create_after_absent_observation(path):
                present = lexists(path)
                if Path(path) == target and not observations:
                    observations.append(present)
                    self.assertFalse(present)
                    target.write_bytes(FOREIGN)
                return present
            with patch.object(books.os.path, 'lexists', side_effect=create_after_absent_observation):
                with self.assertRaisesRegex(ValueError, 'removal target is occupied'):
                    books.recover_book_state_locked(root)
            self.assertEqual([False], observations)
            self.assertEqual(FOREIGN, target.read_bytes())
            self.assertFalse((workspace / 'discard-0').exists())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_generation_expectation_requires_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            with self.assertRaisesRegex(ValueError, 'generation requires an identity-bound move'):
                books._move(
                    source, root / 'old-0', exclusive=True, anchor=root,
                    expected_source_generation=1)
            self.assertEqual(OWNED, source.read_bytes())
            self.assertFalse((root / 'old-0').exists())

    def test_bogus_expected_source_and_parent_refuse_before_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            info = source.stat()
            with self.assertRaisesRegex(ValueError, 'Book artifact changed'):
                books._move(
                    source, root / 'old-0', exclusive=True, anchor=root,
                    expected_source_identity=[info.st_dev, info.st_ino + 999])
            with self.assertRaisesRegex(ValueError, 'Book move parent changed'):
                books._move(
                    source, root / 'old-0', exclusive=True, anchor=root,
                    source_parent_identity=[root.stat().st_dev, root.stat().st_ino + 999])
            self.assertEqual(OWNED, source.read_bytes())
            self.assertFalse((root / 'old-0').exists())

    def test_tuple_source_identity_is_accepted_consistently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            destination = root / 'old-0'
            source.write_bytes(OWNED)
            version = books._book_target_version(source)
            identity = tuple(version[index] for index in (0, 1, 2, 3, 5, 6))
            books._move(source, destination, exclusive=True, anchor=root,
                        expected_source_identity=identity)
            self.assertFalse(source.exists())
            self.assertEqual(OWNED, destination.read_bytes())

    def test_boolean_parent_identity_is_rejected_before_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'artifact'
            source.write_bytes(OWNED)
            with self.assertRaisesRegex(ValueError, 'parent changed'):
                books._move(source, root / 'old-0', exclusive=True, anchor=root,
                            source_parent_identity=[True, root.stat().st_ino])
            self.assertEqual(OWNED, source.read_bytes())
            self.assertFalse((root / 'old-0').exists())

    def test_ensure_recovery_is_bound_to_admitted_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(books, 'recover_book_state_locked', return_value=False) as recover:
                with books.ensure_book_state(root):
                    pass
            identity = (root.stat().st_dev, root.stat().st_ino)
            recover.assert_called_once_with(
                root, cancel_check=None, expected_root=root,
                expected_root_identity=identity)

    def test_recovery_root_expectation_accepts_string_and_rejects_partial_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            identity = (root.stat().st_dev, root.stat().st_ino)
            self.assertFalse(books.recover_book_state_locked(
                str(root), expected_root=str(root), expected_root_identity=identity))
            with self.assertRaisesRegex(ValueError, 'expectation is incomplete'):
                books.recover_book_state_locked(root, expected_root=root)
            with self.assertRaisesRegex(ValueError, 'expectation is incomplete'):
                books.recover_book_state_locked(root, expected_root_identity=identity)
