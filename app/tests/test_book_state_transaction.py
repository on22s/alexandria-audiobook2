import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import book_state_transaction as books


class BookStateTransactionTests(unittest.TestCase):
    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd,
                         'POSIX directory descriptors required')
    def test_failed_initial_journal_fsync_removes_the_owned_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            actual_fsync = os.fsync
            calls = 0
            def fail_first_fsync(fd):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError('injected journal fsync failure')
                return actual_fsync(fd)
            try:
                with patch.object(books.os, 'fsync', side_effect=fail_first_fsync), \
                        self.assertRaisesRegex(OSError, 'injected'):
                    books._save_journal(root, {'version':3}, create=True, root_fd=descriptor)
            finally:
                os.close(descriptor)
            self.assertFalse((root / books.JOURNAL).exists())

    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd,
                         'POSIX directory descriptors required')
    def test_parent_identity_capture_refuses_substitution_and_missing_suffix_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); parent = root/'sub'; parent.mkdir()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                expected = books._capture_target_parent_identities(descriptor, 'sub/file')
                missing = books._capture_target_parent_identities(descriptor, 'absent/child/file')
                self.assertEqual([None, None], missing[1:])
                books._require_target_parent_identities(descriptor, 'sub/file', expected)
                parent.rename(root/'retained'); parent.mkdir()
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._require_target_parent_identities(descriptor, 'sub/file', expected)
                (root/'absent').mkdir()
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._require_target_parent_identities(descriptor, 'absent/child/file', missing)
            finally:
                os.close(descriptor)
    def test_scalar_journal_entry_is_rejected_without_mutation(self):
        for version in (2, 3):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                journal = root / books.JOURNAL
                raw = json.dumps({'version': version, 'phase': 'publishing',
                                  'workspace': '.book-switch-test', 'entries': [42]}).encode()
                journal.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, 'entries'):
                    books.recover_book_state_locked(root)
                self.assertEqual(raw, journal.read_bytes())
    def test_stream_admission_is_bound_to_the_admitted_root_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base/'book'; root.mkdir()
            (root/'audio.wav').write_bytes(b'original')
            retained = base/'retained'
            with books.ensure_book_state(root):
                root.rename(retained); root.mkdir()
                (root/'foreign').write_bytes(b'preserve')
                with self.assertRaisesRegex(ValueError, 'admission'):
                    books.apply_book_state_locked(
                        root, {'audio.wav':books.BookStateStream(
                            io.BytesIO(b'generated'), 9,
                            hashlib.sha256(b'generated').hexdigest())}, [],
                        stream_byte_limit=100)
            self.assertEqual(b'preserve', (root/'foreign').read_bytes())
            self.assertFalse((root/'audio.wav').exists())
            self.assertEqual(b'original', (retained/'audio.wav').read_bytes())

    def test_staged_replacement_change_after_journal_is_refused_before_original_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = root/'script.json'; target.write_bytes(b'original')
            save = books._save_journal; changed = []
            def replace_staged(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing' and not changed:
                    staged = root/document['workspace']/'new-0'
                    staged.unlink(); staged.write_bytes(b'foreign')
                    changed.append(True)
                return result
            with patch.object(books, '_save_journal', side_effect=replace_staged):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'replacement'):
                    books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertEqual(b'original', target.read_bytes())
            self.assertTrue((root/books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture')
    def test_symlinked_book_ancestor_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); real = base/'real'; real.mkdir(); root = real/'book'; root.mkdir()
            alias = base/'alias'; alias.symlink_to(real, target_is_directory=True)
            aliased_root = alias/'book'
            with self.assertRaisesRegex(ValueError, 'direct directory'):
                books.apply_book_state_locked(aliased_root, {'script.json':b'generated'}, [])
            self.assertFalse((root/books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd
                         and os.mkdir in os.supports_dir_fd,
                         'POSIX descriptor-relative staging unavailable')
    def test_root_relocation_during_staging_cannot_publish_into_replacement_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base/'book'; root.mkdir()
            target = root/'script.json'; target.write_bytes(b'original')
            retained = base/'retained-book'; stage = books._stage_book_stream
            relocated = []
            def relocate_root(*args, **kwargs):
                result = stage(*args, **kwargs)
                if not relocated:
                    root.rename(retained)
                    root.mkdir()
                    (root/'foreign').write_bytes(b'do not modify')
                    relocated.append(True)
                return result
            with patch.object(books, '_stage_book_stream', side_effect=relocate_root):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertEqual(b'do not modify', (root/'foreign').read_bytes())
            self.assertFalse((root/'script.json').exists())
            self.assertEqual(b'original', (retained/'script.json').read_bytes())
            journal = json.loads((retained/books.JOURNAL).read_text())
            self.assertEqual(b'generated', (retained/journal['workspace']/'new-0').read_bytes())

    def test_byte_target_changed_before_commit_keeps_publishing_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            validate = books._validate_byte_cleanup_targets
            changed = []
            def change_before_validation(rows, targets, phase):
                if phase == 'committed' and not changed:
                    target.write_bytes(b'writer edit')
                    changed.append(True)
                return validate(rows, targets, phase)
            with patch.object(books, '_validate_byte_cleanup_targets',
                              side_effect=change_before_validation):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            self.assertTrue(changed)
            self.assertEqual(b'writer edit', target.read_bytes())
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual('publishing', journal['phase'])
            self.assertEqual(b'original', (root / journal['workspace'] / 'old-0').read_bytes())

    def test_phase_update_refuses_journal_replaced_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            save = books._save_journal
            foreign = b'{"foreign":"journal"}'
            replaced = []
            def replace_before_update(directory, document, **kwargs):
                if kwargs.get('expected_version') is not None and not replaced:
                    journal = root / books.JOURNAL
                    journal.unlink()
                    journal.write_bytes(foreign)
                    replaced.append(True)
                return save(directory, document, **kwargs)
            with patch.object(books, '_save_journal', side_effect=replace_before_update):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            self.assertTrue(replaced)
            self.assertEqual(foreign, (root / books.JOURNAL).read_bytes())
            self.assertEqual(b'generated', target.read_bytes())
            self.assertEqual(b'original', next(root.glob('.book-switch-*/old-0')).read_bytes())

    @unittest.skipIf(os.name == 'nt', 'Windows junction qualification requires a native test')
    def test_byte_publication_refuses_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'; parent.mkdir()
            save = books._save_journal; swapped = []
            def swap_after_journal(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing' and not swapped:
                    parent.rename(root / 'retained')
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped.append(True)
                return result
            with patch.object(books, '_save_journal', side_effect=swap_after_journal):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'sub/file':b'generated'}, [])
            self.assertTrue(swapped)
            self.assertFalse((outside / 'file').exists())
            self.assertTrue((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'Windows junction qualification requires a native test')
    def test_byte_cleanup_refuses_parent_swapped_after_publication(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'; parent.mkdir()
            target = parent / 'file'; target.write_bytes(b'original')
            move = books._move
            swapped = []
            def swap_after_publish(source, destination, **kwargs):
                result = move(source, destination, **kwargs)
                if Path(destination) == target and not swapped:
                    parent.rename(root / 'retained')
                    (root / 'retained' / 'file').rename(outside / 'file')
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped.append(True)
                return result
            with patch.object(books, '_move', side_effect=swap_after_publish):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'sub/file': b'generated'}, [])
            self.assertTrue(swapped)
            self.assertEqual(b'generated', (outside / 'file').read_bytes())
            self.assertEqual(b'original', next(root.glob('.book-switch-*/old-0')).read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())
            document = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(2, len(document['target_parent_identities'][0]))
            with self.assertRaisesRegex(ValueError, 'parent changed'):
                with books.ensure_book_state(root):
                    self.fail('Recovery followed a substituted target parent')
            self.assertEqual(b'generated', (outside / 'file').read_bytes())
            self.assertEqual(b'original', next(root.glob('.book-switch-*/old-0')).read_bytes())
    def test_byte_transaction_preserves_edit_made_during_original_move(self):
        # A conflict can leave recovery fenced; it must not silently delete the
        # writer's changed backup while publishing the generated replacement.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'script.json'
            target.write_bytes(b'original')
            move = books._move; injected = []
            def edit_before_backup(source, destination, **kwargs):
                if Path(source) == target and not injected:
                    target.write_bytes(b'writer edit'); injected.append(True)
                return move(source, destination, **kwargs)
            with patch.object(books, '_move', side_effect=edit_before_backup):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertTrue(injected)
            journal = json.loads((root / books.JOURNAL).read_text())
            backup = root / journal['workspace'] / 'old-0'
            self.assertEqual(b'writer edit', backup.read_bytes())
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX mode semantics required')
    def test_byte_transaction_preserves_mode_change_during_original_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'script.json'
            target.write_bytes(b'original'); target.chmod(0o600)
            move = books._move; injected = []
            def chmod_before_backup(source, destination, **kwargs):
                if Path(source) == target and not injected:
                    target.chmod(0o640); injected.append(True)
                return move(source, destination, **kwargs)
            with patch.object(books, '_move', side_effect=chmod_before_backup):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            candidates = [p for p in (target, *root.glob('.book-switch-*/old-0')) if p.exists()]
            self.assertEqual(1, len(candidates))
            self.assertEqual(0o640, candidates[0].stat().st_mode & 0o777)
            self.assertEqual(b'original', candidates[0].read_bytes())

    @unittest.skipIf(os.name == 'nt', 'Windows may deny moving an open file')
    def test_byte_transaction_preserves_backup_edited_before_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'script.json'
            target.write_bytes(b'original')
            move = books._move; injected = []
            with target.open('r+b') as writer:
                def edit_after_publish(source, destination, **kwargs):
                    result = move(source, destination, **kwargs)
                    if Path(destination) == target and Path(source).name == 'new-0' and not injected:
                        writer.seek(0); writer.write(b'writer edit'); writer.truncate(); writer.flush()
                        injected.append(True)
                    return result
                with patch.object(books, '_move', side_effect=edit_after_publish):
                    with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                        books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertTrue(injected)
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(b'writer edit', (root / journal['workspace'] / 'old-0').read_bytes())
            self.assertEqual(b'generated', target.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'Windows may deny moving an open file')
    def test_committed_byte_recovery_preserves_backup_modified_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'script.json'
            target.write_bytes(b'original')
            save = books._save_journal; injected = []
            with target.open('r+b') as writer:
                def edit_after_commit(directory, document, **kwargs):
                    result = save(directory, document, **kwargs)
                    if document['phase'] == 'committed' and not injected:
                        writer.seek(0); writer.write(b'writer edit'); writer.truncate(); writer.flush()
                        injected.append(True)
                    return result
                with patch.object(books, '_save_journal', side_effect=edit_after_commit):
                    with books.ensure_book_state(root), self.assertRaises(ValueError):
                        books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertTrue(injected)
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual('committed', journal['phase'])
            self.assertEqual(b'writer edit', (root / journal['workspace'] / 'old-0').read_bytes())
            self.assertEqual(b'generated', target.read_bytes())

    def test_committed_byte_cleanup_refuses_unknown_workspace_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / 'script.json'
            target.write_bytes(b'original')
            save = books._save_journal; injected = []
            def add_unknown(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'committed' and not injected:
                    foreign = root / document['workspace'] / 'foreign'
                    foreign.write_bytes(b'user data'); injected.append(foreign)
                return result
            with patch.object(books, '_save_journal', side_effect=add_unknown):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
            self.assertEqual(b'user data', injected[0].read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_legacy_recovered_cleanup_refuses_unowned_workspace_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / '.book-switch-legacy-fixture'
            workspace.mkdir()
            foreign = workspace / 'user-data.bin'
            foreign.write_bytes(b'preserve me')
            target = root / 'script.json'
            target.write_bytes(b'after')
            (root / books.JOURNAL).write_text(json.dumps({
                'version': 1,
                'phase': 'recovered',
                'workspace': workspace.name,
                'entries': [{
                    'path': target.name,
                    'existed': False,
                    'old_sha256': None,
                    'new_sha256': hashlib.sha256(b'after').hexdigest(),
                }],
            }))
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Unvalidated recovered cleanup was admitted')
            self.assertEqual(b'preserve me', foreign.read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(b'after', target.read_bytes())

    def test_committed_byte_cleanup_refuses_entry_added_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            validate = books._validate_committed_byte_backups
            planted = []
            def add_after_validation(workspace, rows, targets):
                result = validate(workspace, rows, targets)
                foreign = Path(workspace) / 'foreign'
                foreign.write_bytes(b'user data')
                planted.append(foreign)
                return result
            with patch.object(books, '_validate_committed_byte_backups',
                              side_effect=add_after_validation):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            self.assertEqual(1, len(planted))
            self.assertEqual(b'user data', planted[0].read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_cleanup_refuses_to_unlink_replaced_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            cleanup = books._cleanup_validated_byte_workspace
            foreign = b'{"foreign":"journal"}'
            replaced = []
            def replace_after_cleanup(directory, workspace, document, targets, journal_version):
                result = cleanup(directory, workspace, document, targets, journal_version)
                journal = root / books.JOURNAL
                journal.unlink()
                journal.write_bytes(foreign)
                replaced.append(True)
                return result
            with patch.object(books, '_cleanup_validated_byte_workspace',
                              side_effect=replace_after_cleanup):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            self.assertTrue(replaced)
            self.assertEqual(foreign, (root / books.JOURNAL).read_bytes())
            self.assertEqual(b'generated', target.read_bytes())

    def test_recovery_binds_cleanup_to_journal_identity_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'generated')
            workspace = root / '.book-switch-fixture'
            workspace.mkdir()
            backup = workspace / 'old-0'
            backup.write_bytes(b'original')
            old_version = books._book_target_version(backup)
            new_version = books._book_target_version(target)
            document = {
                'version': 3,
                'phase': 'committed',
                'workspace': workspace.name,
                'entries': [{
                    'path': target.name,
                    'existed': True,
                    'old_sha256': hashlib.sha256(b'original').hexdigest(),
                    'new_sha256': hashlib.sha256(b'generated').hexdigest(),
                    'old_file_identity': [old_version[index] for index in (0, 1, 2, 3, 5, 6)],
                    'new_file_identity': [new_version[0], new_version[1]],
                }],
            }
            books._save_journal(root, document)
            validate = books._validate_committed_byte_backups
            foreign = b'{"foreign":"new transaction"}'
            def replace_after_read(workspace_path, rows, targets):
                result = validate(workspace_path, rows, targets)
                journal = root / books.JOURNAL
                journal.unlink()
                journal.write_bytes(foreign)
                return result
            with patch.object(books, '_validate_committed_byte_backups',
                              side_effect=replace_after_read):
                with self.assertRaises(ValueError):
                    with books.ensure_book_state(root):
                        self.fail('Cleanup used a journal replaced after its read')
            self.assertEqual(foreign, (root / books.JOURNAL).read_bytes())
            self.assertEqual(b'original', backup.read_bytes())
            self.assertEqual(b'generated', target.read_bytes())

    def test_byte_directory_cleanup_refuses_nested_entry_added_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'chapters'
            (directory / 'nested').mkdir(parents=True)
            (directory / 'nested' / 'owned').write_bytes(b'owned')
            validate = books._validate_byte_workspace_for_cleanup
            planted = []
            def add_nested(workspace, document):
                result = validate(workspace, document)
                foreign = Path(workspace) / 'old-0' / 'nested' / 'foreign'
                foreign.write_bytes(b'user data')
                planted.append(foreign)
                return result
            with patch.object(books, '_validate_byte_workspace_for_cleanup',
                              side_effect=add_nested):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {}, ['chapters'])
            self.assertEqual(b'user data', planted[0].read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_stream_directory_cleanup_refuses_nested_entry_added_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'chapters'
            (directory / 'nested').mkdir(parents=True)
            (directory / 'nested' / 'owned').write_bytes(b'owned')
            validate = books._validate_stream_workspace_for_cleanup
            planted = []
            def add_nested(workspace, document):
                result = validate(workspace, document)
                foreign = Path(workspace) / 'old-1' / 'nested' / 'foreign'
                foreign.write_bytes(b'user data')
                planted.append(foreign)
                return result
            source = books.BookStateStream(io.BytesIO(b'generated'), len(b'generated'),
                                           hashlib.sha256(b'generated').hexdigest())
            with patch.object(books, '_validate_stream_workspace_for_cleanup',
                              side_effect=add_nested):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': source}, ['chapters'],
                                                  stream_byte_limit=1024)
            self.assertEqual(b'user data', planted[0].read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_legacy_v1_cleanup_requires_explicit_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'generated')
            workspace = root / '.book-switch-legacy'
            workspace.mkdir()
            foreign = workspace / 'old-0'
            foreign.write_bytes(b'original')
            (root / books.JOURNAL).write_text(json.dumps({
                'version': 1,
                'phase': 'committed',
                'workspace': workspace.name,
                'entries': [{
                    'path': target.name,
                    'existed': True,
                    'old_sha256': hashlib.sha256(b'original').hexdigest(),
                    'new_sha256': hashlib.sha256(b'generated').hexdigest(),
                }],
            }))
            with self.assertRaisesRegex(ValueError, 'explicit recovery inspection'):
                with books.ensure_book_state(root):
                    self.fail('Identity-free v1 cleanup was admitted')
            self.assertEqual(b'original', foreign.read_bytes())
            self.assertEqual(b'generated', target.read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_committed_stream_cleanup_rechecks_target_before_deleting_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            validate = books._validate_stream_workspace_for_cleanup
            changed = []
            def change_target_after_validation(workspace, document):
                result = validate(workspace, document)
                if document['phase'] == 'committed' and not changed:
                    target.write_bytes(b'writer edit')
                    changed.append(True)
                return result
            source = books.BookStateStream(io.BytesIO(b'generated'), len(b'generated'),
                                           hashlib.sha256(b'generated').hexdigest())
            with patch.object(books, '_validate_stream_workspace_for_cleanup',
                              side_effect=change_target_after_validation):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'script.json': source}, [],
                                                  stream_byte_limit=1024)
            self.assertTrue(changed)
            self.assertEqual(b'writer edit', target.read_bytes())
            backups = list(root.glob('.book-switch-*/old-0'))
            self.assertEqual(1, len(backups))
            self.assertEqual(b'original', backups[0].read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_byte_prejournal_failure_preserves_unowned_workspace_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = books._stage_book_stream
            def stage_then_foreign(path, value, *args, **kwargs):
                result = stage(path, value, *args, **kwargs)
                if Path(path).name == 'new-0':
                    directory_fd = kwargs.get('directory_fd')
                    descriptor = os.open('foreign', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=directory_fd)
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(b'user data')
                    raise OSError('fixture staging failure')
                return result
            with patch.object(books, '_stage_book_stream',
                              side_effect=stage_then_foreign):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            survivors = list(root.glob('.book-switch-*/foreign'))
            self.assertEqual(1, len(survivors))
            self.assertEqual(b'user data', survivors[0].read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'symlink creation may require privileges')
    def test_recovery_refuses_dangling_symlink_target_and_keeps_staged_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            save = books._save_journal
            injected = []
            def plant_link(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing' and not injected:
                    target.symlink_to(root / 'missing')
                    injected.append(True)
                return result
            with patch.object(books, '_save_journal', side_effect=plant_link):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            self.assertTrue(injected)
            self.assertTrue(target.is_symlink())
            self.assertEqual(1, len(list(root.glob('.book-switch-*/new-0'))))
            self.assertTrue((root / books.JOURNAL).exists())
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Recovery consumed an occupied target')

    def test_committed_byte_cleanup_preserves_backup_when_target_changed(self):
        for remove in (False, True):
            with self.subTest(remove=remove), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); target = root / 'script.json'
                target.write_bytes(b'original')
                save = books._save_journal; injected = []
                def change_target(directory, document, **kwargs):
                    result = save(directory, document, **kwargs)
                    if document['phase'] == 'committed' and not injected:
                        if remove:
                            target.unlink()
                        else:
                            target.write_bytes(b'writer edit')
                        injected.append(True)
                    return result
                with patch.object(books, '_save_journal', side_effect=change_target):
                    with books.ensure_book_state(root), self.assertRaises(ValueError):
                        books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
                journal = json.loads((root / books.JOURNAL).read_text())
                self.assertEqual(b'original', (root / journal['workspace'] / 'old-0').read_bytes())
                if remove:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(b'writer edit', target.read_bytes())

    def test_byte_transaction_preserves_target_created_during_publication(self):
        # This tests only preservation on conflict. The resulting journal still
        # needs a separately designed, resumable conflict-resolution path.
        for existed in (False, True):
            with self.subTest(existed=existed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); target = root / 'script.json'
                if existed:
                    target.write_bytes(b'original')
                move, save = books._move, books._save_journal
                injected = []
                def after_move(source, destination, **kwargs):
                    result = move(source, destination, **kwargs)
                    if existed and Path(source) == target and not injected:
                        target.write_bytes(b'writer'); injected.append(True)
                    return result
                def after_journal(directory, data, **kwargs):
                    result = save(directory, data, **kwargs)
                    if not existed and data['phase'] == 'publishing' and not injected:
                        target.write_bytes(b'writer'); injected.append(True)
                    return result
                with patch.object(books, '_move', side_effect=after_move), \
                     patch.object(books, '_save_journal', side_effect=after_journal):
                    with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                        books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
                self.assertTrue(injected)
                self.assertEqual(b'writer', target.read_bytes())
                self.assertTrue((root / books.JOURNAL).exists())
                if existed:
                    journal = json.loads((root / books.JOURNAL).read_text())
                    self.assertEqual(b'original', (root / journal['workspace'] / 'old-0').read_bytes())

    def test_byte_recovery_preserves_new_target_even_if_bytes_match_staged_output(self):
        for existed in (False, True):
            with self.subTest(existed=existed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); target = root / 'script.json'
                if existed:
                    target.write_bytes(b'original')
                move, save = books._move, books._save_journal
                injected = []
                def after_move(source, destination, **kwargs):
                    result = move(source, destination, **kwargs)
                    if existed and Path(source) == target and not injected:
                        target.write_bytes(b'generated'); injected.append(True)
                    return result
                def after_journal(directory, data, **kwargs):
                    result = save(directory, data, **kwargs)
                    if not existed and data['phase'] == 'publishing' and not injected:
                        target.write_bytes(b'generated'); injected.append(True)
                    return result
                with patch.object(books, '_move', side_effect=after_move), \
                     patch.object(books, '_save_journal', side_effect=after_journal):
                    with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                        books.apply_book_state_locked(root, {'script.json':b'generated'}, [])
                self.assertTrue(injected)
                self.assertEqual(b'generated', target.read_bytes())
                self.assertTrue((root / books.JOURNAL).exists())
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        with books.ensure_book_state(root):
                            self.fail('Unowned target was admitted')
                    self.assertEqual(b'generated', target.read_bytes())
                if existed:
                    journal = json.loads((root / books.JOURNAL).read_text())
                    self.assertEqual(b'original', (root / journal['workspace'] / 'old-0').read_bytes())

    def test_byte_recovery_preserves_same_content_foreign_target_after_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'script.json'
            target.write_bytes(b'original')
            move = books._move
            def interrupt_after_publication(source, destination, **kwargs):
                result = move(source, destination, **kwargs)
                if Path(source).name == 'new-0' and Path(destination).name == target.name:
                    raise OSError('fixture publication interruption')
                return result
            with books.ensure_book_state(root), \
                 patch.object(books, '_move', side_effect=interrupt_after_publication), \
                 patch.object(books, 'recover_book_state_locked', side_effect=SystemExit('fixture process exit')), \
                 self.assertRaises(SystemExit):
                    books.apply_book_state_locked(root, {'script.json': b'generated'}, [])
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(3, journal['version'])
            target.unlink()
            target.write_bytes(b'generated')
            foreign_identity = (target.stat().st_dev, target.stat().st_ino)
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Same-content foreign target was admitted')
            self.assertEqual(foreign_identity, (target.stat().st_dev, target.stat().st_ino))
            self.assertEqual(b'generated', target.read_bytes())
            self.assertEqual(b'original',
                             (root / journal['workspace'] / 'old-0').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_oversized_or_duplicate_journal_is_refused_without_mutation(self):
        for raw in (b' ' * 65, b'{"version":1,"version":2}'):
            with self.subTest(size=len(raw)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                journal = root / books.JOURNAL
                target = root / 'user-file'
                journal.write_bytes(raw)
                target.write_bytes(b'preserve')
                with patch.object(books, 'MAX_BOOK_JOURNAL_BYTES', 64):
                    with self.assertRaises(ValueError):
                        with books.ensure_book_state(root):
                            self.fail('Invalid journal was admitted')
                self.assertEqual(raw, journal.read_bytes())
                self.assertEqual(b'preserve', target.read_bytes())

    def test_journal_writer_refuses_oversize_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(books, 'MAX_BOOK_JOURNAL_BYTES', 64):
                with self.assertRaisesRegex(ValueError, 'journal limit'):
                    books._save_journal(root, {'content': 'x' * 100})
            self.assertFalse((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'Windows symlink creation may require privileges')
    def test_dangling_journal_link_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            journal.symlink_to(root / 'missing')
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Journal link was treated as absent')
            self.assertTrue(journal.is_symlink())

    def prepare(self, root):
        (root/'script.json').write_bytes(b'old script')
        (root/'voice.json').write_bytes(b'old voices')
        (root/'state.json').write_bytes(b'old identity')
        (root/'chapters').mkdir()
        (root/'chapters/audio.wav').write_bytes(b'old chapter audio')
        (root/'export.mp3').write_bytes(b'old export')
        (root/'checkpoint.json').write_bytes(b'old checkpoint')
        return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def snapshot(self, root):
        return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    def switch(self, root):
        with books.ensure_book_state(str(root)):
            books.apply_book_state_locked(str(root), {'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'}, ['chapters','export.mp3','checkpoint.json'])

    def test_success_publishes_complete_set_and_removes_prior_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.prepare(root);self.switch(root)
            self.assertEqual({'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'},self.snapshot(root))

    def test_move_failure_restores_exact_previous_book_exports_and_checkpoint(self):
        for failure_at in range(1,11):
            with self.subTest(move=failure_at),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);originals=self.prepare(root);move=books._move;counter=0
                def fail_once(source,destination, **kwargs):
                    nonlocal counter
                    counter+=1
                    if counter==failure_at:raise OSError('fixture publication disk full')
                    return move(source,destination, **kwargs)
                with patch.object(books,'_move',side_effect=fail_once):
                    with self.assertRaisesRegex(OSError,'publication disk full'):self.switch(root)
                self.assertEqual(originals,self.snapshot(root))

    def test_process_death_recovers_idempotently_in_fresh_process(self):
        worker='''
import os,sys
from pathlib import Path
import book_state_transaction as books
root=Path(sys.argv[1]);boundary=sys.argv[2];point=int(sys.argv[3]);count=0
original=books._move if boundary=='move' else books._save_journal
def exit_after(*args,**kwargs):
 global count
 result=original(*args,**kwargs);count+=1
 if count==point:os._exit(77)
 return result
if boundary=='move':books._move=exit_after
else:books._save_journal=exit_after
with books.ensure_book_state(str(root)):
 books.apply_book_state_locked(str(root),{'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'},['chapters','export.mp3','checkpoint.json'])
'''
        recovery="import sys;from book_state_transaction import ensure_book_state;\nwith ensure_book_state(sys.argv[1]):pass"
        cases=[('move',n) for n in range(1,11)]+[('journal',1),('journal',2)]
        for boundary,point in cases:
            with self.subTest(boundary=boundary,point=point),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);originals=self.prepare(root)
                result=subprocess.run([sys.executable,'-c',worker,tmp,boundary,str(point)],capture_output=True,text=True,timeout=15)
                self.assertEqual(77,result.returncode,result.stderr)
                for repeat in range(2):
                    result=subprocess.run([sys.executable,'-c',recovery,tmp],capture_output=True,text=True,timeout=15)
                    self.assertEqual(0,result.returncode,result.stderr)
                expected={'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'} if boundary=='journal' and point==2 else originals
                self.assertEqual(expected,self.snapshot(root))

    def test_corrupt_original_fails_before_restoring_any_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.prepare(root);move=books._move;counter=0
            def leave_pending(source,destination, **kwargs):
                nonlocal counter
                move(source,destination, **kwargs);counter+=1
                if counter==3:raise SystemExit('fixture stop')
            with patch.object(books,'_move',side_effect=leave_pending),patch.object(books,'recover_book_state_locked'):
                with self.assertRaises(SystemExit):self.switch(root)
            data=json.loads((root/books.JOURNAL).read_text());original=root/data['workspace']/'old-0';original.write_bytes(b'corrupt snapshot')
            before=self.snapshot(root)
            with self.assertRaisesRegex(ValueError,'missing or changed'):
                with books.ensure_book_state(tmp):pass
            self.assertEqual(before,self.snapshot(root))

    def test_external_symlink_and_duplicate_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);other=Path(outside)/'protected';other.write_bytes(b'protected')
            (root/'link').symlink_to(other)
            for replacements,removals in [({'../protected':b'bad'},[]),({'link':b'bad'},[]),({'same':b'bad'},['same'])]:
                with self.subTest(replacements=replacements),self.assertRaises(ValueError):
                    with books.ensure_book_state(tmp):books.apply_book_state_locked(tmp,replacements,removals)
            self.assertEqual(b'protected',other.read_bytes())


    def test_changed_replacement_refuses_recovery_before_deleting_any_artifact(self):
        worker = """
import os,sys
from pathlib import Path
import book_state_transaction as books
root=Path(sys.argv[1]);move=books._move;count=0
def crash_after_publish(source,destination, **kwargs):
 global count
 move(source,destination, **kwargs);count+=1
 if count==10:os._exit(77)
books._move=crash_after_publish
with books.ensure_book_state(str(root)):
 books.apply_book_state_locked(str(root),{'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'},['chapters','export.mp3','checkpoint.json'])
"""
        # Protect both a replacement with an old backup and a newly created file.
        for name in ('voice.json', 'new.json'):
            with self.subTest(edited=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.prepare(root)
                result = subprocess.run([sys.executable, '-c', worker, tmp],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(77, result.returncode, result.stderr)
                journal = json.loads((root / books.JOURNAL).read_bytes())
                self.assertEqual('publishing', journal['phase'])
                workspace = root / journal['workspace']
                self.assertEqual(b'old script', (workspace / 'old-0').read_bytes())
                self.assertEqual(b'new script', (root / 'script.json').read_bytes())
                self.assertEqual(b'new voices' if name == 'voice.json' else b'new only',
                                 (root / name).read_bytes())
                (root / name).write_bytes(b'human edit after the crash')
                before = self.snapshot(root)
                for retry in range(2):
                    with self.assertRaisesRegex(ValueError, 'Active-book replacement changed; refusing destructive recovery'):
                        with books.ensure_book_state(tmp):
                            self.fail('Recovery admitted a book with an edited replacement')
                    # Refusal must preserve all current files, every old backup,
                    # and the pending journal, not just the edited target.
                    self.assertEqual(before, self.snapshot(root))
                    self.assertEqual(b'human edit after the crash', (root / name).read_bytes())


class SavedBookTransactionHttpTests(unittest.TestCase):
    def test_http_load_failure_preserves_prior_book_and_success_publishes_all(self):
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib
        from project import CHAPTER_EXPORT_DIR
        for mode in ("success", "move_failure", "bad_voices"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);saved=root/'scripts';saved.mkdir()
                (saved/'new.json').write_bytes(b'[{"speaker":"NEW","text":"new book"}]')
                (saved/'new.voice_config.json').write_bytes(b'[]' if mode=='bad_voices' else b'{"NEW":{"voice":"Aiden"}}')
                active=root/'annotated_script.json';active.write_bytes(b'[{"speaker":"OLD","text":"old book"}]')
                voice=root/'voice_config.json';voice.write_bytes(b'{"OLD":{"voice":"Ryan"}}')
                state=root/'state.json';state.write_bytes(b'{"active_book_id":"old","keep":17}')
                export=root/'audiobook.mp3';export.write_bytes(b'old export')
                chunks=root/'chunks.json';chunks.write_bytes(b'[{"id":1}]')
                chunk_journal=root/'chunks.json.status.jsonl';chunk_journal.write_bytes(b'old chunk status journal')
                from generation_checkpoint_shards import GenerationCheckpointShards
                from generate_script import get_generation_checkpoint_path
                generation=Path(get_generation_checkpoint_path(str(active)))
                generation_writer=GenerationCheckpointShards(generation, {'source_sha256':'old'})
                generation_writer.save_chunks([{'entries':[{'text':'accepted old source'}]}])
                generation_shard=generation_writer.directory/'00000000.json'
                chapters=root/CHAPTER_EXPORT_DIR;chapters.mkdir();(chapters/'part.mp3').write_bytes(b'chapter bytes')
                checkpoint=Path(str(active)+'.review_checkpoint.json');checkpoint.write_bytes(b'{"old":true}')
                originals={p:p.read_bytes() for p in (active,voice,state,export,chunks,chunk_journal,generation,generation_shard,chapters/'part.mp3',checkpoint)}
                app=FastAPI();app.include_router(lib.router)
                with ExitStack() as stack:
                    for name,value in (("SCRIPT_PATH",str(active)),("VOICE_CONFIG_PATH",str(voice)),("DATA_DIR",tmp),("SCRIPTS_DIR",str(saved)),("CHUNKS_PATH",str(chunks)),("AUDIOBOOK_PATH",str(export)),("M4B_PATH",str(root/'audiobook.m4b')),("process_state",{})):
                        stack.enter_context(patch.object(lib,name,value))
                    stack.enter_context(patch.object(lib,'_get_saved_book_id',return_value='new-book'))
                    if mode=='move_failure':
                        move=books._move;count=0
                        def fail_once(source,destination, **kwargs):
                            nonlocal count
                            count+=1
                            if count==4:raise OSError('fixture load disk full')
                            return move(source,destination, **kwargs)
                        stack.enter_context(patch.object(books,'_move',side_effect=fail_once))
                    client=stack.enter_context(TestClient(app,raise_server_exceptions=False))
                    response=client.post('/api/scripts/load',json={'name':'new'})
                if mode=='success':
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertEqual((saved/'new.json').read_bytes(),active.read_bytes())
                    self.assertEqual((saved/'new.voice_config.json').read_bytes(),voice.read_bytes())
                    identity=json.loads(state.read_text());self.assertEqual('new-book',identity['active_book_id']);self.assertEqual(17,identity['keep']);self.assertEqual(32,len(identity['book_generation']))
                    for path in (export,chunks,chunk_journal,generation,generation_writer.directory,chapters,checkpoint):self.assertFalse(path.exists())
                else:
                    self.assertEqual(500 if mode=='move_failure' else 400,response.status_code,response.text)
                    for path,expected in originals.items():self.assertEqual(expected,path.read_bytes())
                self.assertFalse((root/books.JOURNAL).exists())
                self.assertEqual([],list(root.glob('.book-switch-*')))


class BookPersonaBindingTests(unittest.TestCase):
    def test_reselecting_same_source_returns_distinct_committed_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = books.apply_book_input_selection(tmp, 'same.txt', 'same')
            self.assertEqual(first, books.get_book_operation_token(tmp))
            second = books.apply_book_input_selection(tmp, 'same.txt', 'same')
            self.assertEqual(second, books.get_book_operation_token(tmp))
            self.assertNotEqual(first, second)

    def test_actual_persona_main_rejects_changed_book_and_keeps_original_artifact_identity(self):
        from contextlib import ExitStack
        import array
        import wave
        from types import SimpleNamespace
        import generate_personas as personas
        for advanced in (False, True):
            with self.subTest(advanced=advanced),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'annotated_script.json').write_bytes(b'[{"speaker":"ALICE","text":"Original book source."}]')
                (root/'voice_config.json').write_bytes(b'{"ALICE":{"description":"initial","keep":17}}')
                (root/'state.json').write_bytes(b'{"active_book_id":"original book","book_generation":"original"}')
                source=root/'rendered.wav'
                with wave.open(str(source),'wb') as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(array.array('h',[1000]*600).tobytes())
                next_voices=b'{"ALICE":{"description":"different book human voice","keep":17}}'
                def render(**kwargs):
                    with books.ensure_book_state(tmp):
                        books.apply_book_state_locked(tmp,{'annotated_script.json':b'[{"speaker":"ALICE","text":"Different book source."}]','voice_config.json':next_voices,'state.json':b'{"active_book_id":"different book","book_generation":"different"}'},[])
                    return str(source),None
                with ExitStack() as stack:
                    for name,value in (("get_runtime_data_dir",tmp),("load_app_config",{}),("get_active_llm_config",{"model_name":"fixture","base_url":"http://unused.invalid"}),("ensure_ideal_settings",(False,{"context_length":4096},"fixture")),("make_run_client",object()),("llm_timeout_seconds",30),("TTSEngine",SimpleNamespace(generate_voice_design=render)),("call_llm_for_object",{"description":"Warm natural voice.","ref_text":"Original book source."})):
                        stack.enter_context(patch.object(personas,name,return_value=value))
                    stack.enter_context(patch.object(personas.time,'sleep'))
                    stack.enter_context(patch.object(sys,'argv',['generate_personas.py']+(['--advanced'] if advanced else [])))
                    with self.assertRaisesRegex(ValueError,'Active book changed'):personas.main()
                self.assertEqual(next_voices,(root/'voice_config.json').read_bytes())
                metas=list((root/'designed_voices/persona').rglob('meta.json'));self.assertEqual(1,len(metas))
                self.assertEqual('original book',json.loads(metas[0].read_text())['book_id'])
                if advanced:
                    basename=Path(personas._character_ref_path('', 'ALICE')).name
                    refs=list((root/'persona_refs').rglob(basename));self.assertEqual(1,len(refs));self.assertEqual(['Original book source.'],json.loads(refs[0].read_text())['sample_lines'])

    def test_same_book_reload_nonce_rejects_and_newer_human_fields_are_preserved(self):
        import copy
        import generate_personas as personas
        from utils import atomic_json_write,file_lock
        for change in ('reload','human_edit'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'annotated_script.json').write_bytes(b'[]');(root/'state.json').write_bytes(b'{"active_book_id":"same","book_generation":"one"}')
                path=root/'voice_config.json';initial={'ALICE':{'description':'old','keep':17,'seed':3}};atomic_json_write(initial,str(path))
                with books.ensure_book_state(tmp):snapshot=books.get_book_snapshot(tmp)
                generated=copy.deepcopy(initial);generated['ALICE'].update(description='generated',persona_status='generated')
                if change=='reload':
                    with books.ensure_book_state(tmp):books.apply_book_state_locked(tmp,{'state.json':b'{"active_book_id":"same","book_generation":"two"}'},[])
                    before=path.read_bytes()
                    with self.assertRaisesRegex(ValueError,'Active book changed'):personas.save_generated_voice_config(str(path),generated,initial,['ALICE'],{},book_snapshot=snapshot)
                    self.assertEqual(before,path.read_bytes())
                else:
                    with file_lock(str(path)):atomic_json_write({'ALICE':{'description':'human latest','keep':17,'seed':0}},str(path))
                    result=personas.save_generated_voice_config(str(path),generated,initial,['ALICE'],{},book_snapshot=snapshot)
                    self.assertEqual('human latest',result['ALICE']['description']);self.assertEqual(0,result['ALICE']['seed']);self.assertEqual('generated',result['ALICE']['persona_status'])

    def test_application_startup_recovers_before_chunk_reset(self):
        import asyncio
        import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'script.json').write_bytes(b'old script');(root/'state.json').write_bytes(b'old identity')
            move=books._move
            def die(source,destination, **kwargs):
                move(source,destination, **kwargs)
                if destination.name=='script.json':raise SystemExit('fixture interruption')
            with books.ensure_book_state(tmp),patch.object(books,'_move',side_effect=die),patch.object(books,'recover_book_state_locked'):
                with self.assertRaises(SystemExit):books.apply_book_state_locked(tmp,{'script.json':b'new script','state.json':b'new identity'},[])
            self.assertEqual(b'new script',(root/'script.json').read_bytes())
            def reset():
                self.assertEqual(b'old script',(root/'script.json').read_bytes());self.assertEqual(b'old identity',(root/'state.json').read_bytes());self.assertFalse((root/books.JOURNAL).exists())
            async def start():
                async with app_module.lifespan(app_module.app):pass
            with patch.object(app_module,'DATA_DIR',tmp),patch.object(app_module,'reset_stuck_chunks',side_effect=reset) as check,patch.object(app_module,'mark_interrupted_runs',return_value=0),patch.object(app_module.evaluation_reviews,'prune_sessions',return_value=0):asyncio.run(start())
            check.assert_called_once()


class BookReaderAdmissionTests(unittest.TestCase):
    def test_http_voice_snapshot_waits_for_complete_switch(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib, voices
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);saved=root/'scripts';saved.mkdir()
            active=root/'annotated_script.json';active.write_bytes(b'[{"speaker":"OLD","text":"old"}]')
            voice=root/'voice_config.json';voice.write_bytes(b'{"OLD":{"type":"custom","voice":"Ryan"}}')
            (saved/'new.json').write_bytes(b'[{"speaker":"NEW","text":"new"}]')
            (saved/'new.voice_config.json').write_bytes(b'{"NEW":{"type":"custom","voice":"Aiden"}}')
            app=FastAPI();app.include_router(lib.router);app.include_router(voices.router)
            paused=threading.Event();release=threading.Event();reader_entered=threading.Event()
            move=books._move;read=voices._ensure_voice_listing
            def pause_mid_switch(source,destination, **kwargs):
                move(source,destination, **kwargs)
                if destination==active:
                    paused.set()
                    if not release.wait(5):raise TimeoutError('fixture did not release switch')
            def guarded_read():
                reader_entered.set()
                return read()
            with ExitStack() as stack:
                for module,values in ((lib,{'SCRIPT_PATH':str(active),'VOICE_CONFIG_PATH':str(voice),'DATA_DIR':tmp,'SCRIPTS_DIR':str(saved),'CHUNKS_PATH':str(root/'chunks.json'),'AUDIOBOOK_PATH':str(root/'audiobook.mp3'),'M4B_PATH':str(root/'audiobook.m4b'),'process_state':{}}),(voices,{'SCRIPT_PATH':str(active),'VOICE_CONFIG_PATH':str(voice)})):
                    for name,value in values.items():stack.enter_context(patch.object(module,name,value))
                stack.enter_context(patch.object(lib,'_get_saved_book_id',return_value='new'))
                stack.enter_context(patch.object(books,'_move',side_effect=pause_mid_switch))
                stack.enter_context(patch.object(voices,'_ensure_voice_listing',side_effect=guarded_read))
                first=stack.enter_context(TestClient(app));second=stack.enter_context(TestClient(app))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    load=pool.submit(first.post,'/api/scripts/load',json={'name':'new'})
                    try:
                        self.assertTrue(paused.wait(5))
                        response=pool.submit(second.get,'/api/voices')
                        self.assertTrue(reader_entered.wait(5))
                        self.assertFalse(response.done())
                    finally:release.set()
                    self.assertEqual(200,load.result(timeout=10).status_code)
                    listing=response.result(timeout=10)
                    self.assertEqual(200,listing.status_code)
                    self.assertEqual([{'name':'NEW','config':{'type':'custom','voice':'Aiden'},'line_count':1,'persona_pending':True}],listing.json())
