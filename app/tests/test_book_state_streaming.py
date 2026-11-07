"""Recoverable book transaction staging for bounded, pinned binary streams."""
import errno
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import book_state_transaction as books
from audio_generation_owner import AudioGenerationCancelled


def stream(data):
    return books.BookStateStream(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


class BookStateStreamingTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor-relative cleanup')
    def test_stream_cleanup_refuses_root_swap_after_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'book'
            root.mkdir()
            workspace = root / '.book-switch-fixture'
            workspace.mkdir()
            (workspace / 'old-0').write_bytes(b'protected')
            original_open = os.open
            supports = set(os.supports_dir_fd)
            swapped = []
            def open_then_swap(path, flags, *args, **kwargs):
                descriptor = original_open(path, flags, *args, **kwargs)
                if Path(path) == root and not swapped:
                    root.rename(base / 'displaced')
                    root.mkdir()
                    swapped.append(True)
                return descriptor
            with patch.object(books, '_verify_stream_cleanup_targets'), \
                 patch.object(books, '_require_book_journal_version'), \
                 patch.object(books, '_validate_stream_workspace_for_cleanup', return_value={}), \
                 patch.object(books.os, 'open', side_effect=open_then_swap) as mocked_open:
                with patch.object(books.os, 'supports_dir_fd', supports | {mocked_open}):
                    with self.assertRaisesRegex(ValueError, 'root changed'):
                        books._cleanup_validated_stream_workspace(root, workspace, {}, None)
            self.assertTrue(swapped)
            self.assertEqual(b'protected', (base / 'displaced' / workspace.name / 'old-0').read_bytes())
            self.assertEqual([], list(root.iterdir()))

    def test_windows_cleanup_inventory_bounds_directory_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(8):
                (root / str(index)).write_bytes(b'')
            real_scandir = os.scandir
            observed = []
            class BoundedScan:
                def __init__(self, path):
                    self.scan = real_scandir(path)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.scan.close()
                def __iter__(self):
                    for entry in self.scan:
                        observed.append(entry.name)
                        if len(observed) > 3:
                            raise AssertionError('enumerated beyond limit plus one')
                        yield entry
            with patch.object(books, 'MAX_LEGACY_CLEANUP_ENTRIES', 2), \
                 patch.object(books.os, 'scandir', side_effect=BoundedScan):
                with self.assertRaisesRegex(ValueError, 'entry count'):
                    books._capture_cleanup_tree_path(root)
            self.assertEqual(3, len(observed))

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor-relative workspace creation')
    def test_workspace_name_collision_preserves_existing_directory(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / '.book-switch-fixture'
            existing.mkdir()
            with books.ensure_book_state(root), \
                 patch.object(books.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixture')):
                with self.assertRaises(FileExistsError):
                    books.apply_book_state_locked(root, {'file': stream(b'new')}, [], stream_byte_limit=100)
            self.assertTrue(existing.is_dir())
            self.assertEqual([], list(existing.iterdir()))
            self.assertFalse((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_parent_identity_walk_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root.stat()
            names = [f'file-{i}' for i in range(10)]
            evidence = [[[info.st_dev, info.st_ino]] for _ in names]
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 5
            with self.assertRaises(AudioGenerationCancelled):
                books._require_transaction_parent_identities(root, names, evidence, cancel_check=cancel)
            self.assertEqual(5, len(checks))

    def test_missing_workspace_recheck_honors_recovery_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'file'
            target.write_bytes(b'generated')
            info = target.stat()
            data = {'version': 2, 'phase': 'committed', 'workspace': '.book-switch-missing',
                    'stream_limits': {'bytes': 100, 'entries': 10},
                    'entries': [{'path': 'file', 'existed': False, 'old_sha256': None,
                                 'new_sha256': hashlib.sha256(b'generated').hexdigest(),
                                 'new_file_identity': [info.st_dev, info.st_ino]}]}
            books._save_journal(root, data)
            verify = books._verify_committed_stream_targets
            cancelled = []
            def cancel_after_initial_validation(*args):
                result = verify(*args)
                cancelled.append(True)
                return result
            with patch.object(books, '_verify_committed_stream_targets', side_effect=cancel_after_initial_validation):
                with self.assertRaises(AudioGenerationCancelled):
                    with books.ensure_book_state(root, cancel_check=lambda: bool(cancelled)):
                        self.fail('Cancelled recovery was admitted')
            self.assertTrue(cancelled)
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(b'generated', target.read_bytes())

    def test_byte_recovery_digest_honors_cancellation_between_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'file'
            payload = b'x' * (2 * 1024 * 1024 + 1)
            target.write_bytes(payload)
            version = books._book_target_version(target)
            row = {
                'new_file_identity': [version[0], version[1]],
                'new_sha256': hashlib.sha256(payload).hexdigest(),
            }
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 4
            with self.assertRaises(AudioGenerationCancelled):
                books._validate_moved_byte_replacement(row, target, cancel_check=cancel)
            self.assertEqual(4, len(checks))

    def test_byte_recovery_directory_digest_preserves_non_ascii_native_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'bundle'
            target.mkdir()
            (target / 'café.txt').write_bytes(b'content')
            expected = books._get_digest(target)
            checks = []
            actual = books._get_digest(target, cancel_check=lambda: checks.append(True) and False)
            self.assertEqual(expected, actual)
            self.assertGreaterEqual(len(checks), 3)

    def test_committed_byte_backup_validation_honors_digest_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            workspace = root / 'missing-workspace'
            payload = b'x' * (2 * 1024 * 1024 + 1)
            target.write_bytes(payload)
            version = books._book_target_version(target)
            row = {
                'existed': False,
                'old_sha256': None,
                'new_sha256': hashlib.sha256(payload).hexdigest(),
                'new_file_identity': [version[0], version[1]],
            }
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 5
            with self.assertRaises(AudioGenerationCancelled):
                books._validate_committed_byte_backups(
                    workspace, [row], [target], cancel_check=cancel)
            self.assertEqual(5, len(checks))

    def test_committed_byte_workspace_enumeration_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'foreign').write_bytes(b'foreign')
            target = root / 'absent'
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 3
            with self.assertRaises(AudioGenerationCancelled):
                books._validate_committed_byte_backups(
                    workspace,
                    [{'existed': False, 'new_sha256': None}],
                    [target], cancel_check=cancel)
            self.assertEqual(3, len(checks))

    def test_byte_cleanup_inventory_honors_cancellation_during_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'bundle'
            target.mkdir()
            for index in range(8):
                (target / f'{index}.bin').write_bytes(b'x')
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 4
            with self.assertRaises(AudioGenerationCancelled):
                books._cleanup_tree_inventory(target, cancel_check=cancel)
            self.assertEqual(4, len(checks))

    def test_stream_workspace_cleanup_validation_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            rows = []
            for index in range(5):
                path = workspace / f'old-{index}'
                path.write_bytes(b'')
                version = books._book_target_version(path)
                rows.append({
                    'existed': True,
                    'old_sha256': hashlib.sha256(b'').hexdigest(),
                    'old_file_identity': [version[item] for item in (0, 1, 2, 3, 5, 6)],
                    'new_sha256': None,
                })
            data = {'phase': 'committed', 'entries': rows,
                    'stream_limits': {'bytes': 100, 'entries': 20}}
            checks = []
            def cancel():
                checks.append(True)
                return len(checks) == 3
            with self.assertRaises(AudioGenerationCancelled):
                books._validate_stream_workspace_for_cleanup(
                    workspace, data, cancel_check=cancel)
            self.assertEqual(3, len(checks))

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_committed_removal_preserves_evidence_if_parent_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / 'sub'
            parent.mkdir()
            (parent / 'old').write_bytes(b'original')
            save = books._save_journal
            removed = []
            def remove_parent_after_commit(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'committed' and not removed:
                    parent.rmdir()
                    removed.append(True)
                return result
            with patch.object(books, '_save_journal', side_effect=remove_parent_after_commit):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'parent changed'):
                    books.apply_book_state_locked(root, {'new': stream(b'generated')}, ['sub/old'],
                                                  stream_byte_limit=100)
            self.assertTrue(removed)
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(b'original', (root / journal['workspace'] / 'old-1').read_bytes())
            self.assertEqual(b'generated', (root / 'new').read_bytes())
            with self.assertRaisesRegex(ValueError, 'parent changed'):
                with books.ensure_book_state(root):
                    self.fail('Changed parent must be inspected before recovery')
            self.assertTrue((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_missing_workspace_rechecks_parent_before_retiring_journal(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'
            parent.mkdir()
            target = parent / 'file'
            target.write_bytes(b'generated')
            info = target.stat()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parents = [books._capture_target_parent_identities(descriptor, 'sub/file')]
            finally:
                os.close(descriptor)
            data = {'version': 2, 'phase': 'committed', 'workspace': '.book-switch-missing',
                    'target_parent_identities': parents, 'stream_limits': {'bytes': 100, 'entries': 10},
                    'entries': [{'path': 'sub/file', 'existed': False, 'old_sha256': None,
                                 'new_sha256': hashlib.sha256(b'generated').hexdigest(),
                                 'new_file_identity': [info.st_dev, info.st_ino]}]}
            books._save_journal(root, data)
            verify = books._verify_committed_stream_targets
            swapped = []
            def swap_after_validation(*args):
                result = verify(*args)
                if not swapped:
                    parent.rename(outside / 'moved')
                    parent.symlink_to(outside / 'moved', target_is_directory=True)
                    swapped.append(True)
                return result
            with patch.object(books, '_verify_committed_stream_targets', side_effect=swap_after_validation):
                with self.assertRaises((OSError, ValueError)):
                    with books.ensure_book_state(root):
                        self.fail('Changed parent was admitted')
            self.assertTrue(swapped)
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(b'generated', (outside / 'moved' / 'file').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_root_level_parent_checks_use_the_same_pinned_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root.stat()
            expected = [[[info.st_dev, info.st_ino]], [[info.st_dev, info.st_ino]]]
            with patch.object(books, '_capture_target_parent_identities',
                              side_effect=AssertionError('root parent should not be reopened')):
                books._require_transaction_parent_identities(root, ['one', 'two'], expected)
                expected[1][0][1] += 1
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._require_transaction_parent_identities(root, ['one', 'two'], expected)
            for name in ('..', '/', '//', ''):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Invalid book artifact path'):
                    books._require_transaction_parent_identities(root, [name], [[[info.st_dev, info.st_ino]]])
            with patch.object(books.os, 'name', 'nt'):
                with self.assertRaisesRegex(OSError, 'Anchored parent capture is unavailable'):
                    books._require_transaction_parent_identities(root, ['one'],
                                                                  [[[info.st_dev, info.st_ino]]])

    @unittest.skipUnless(hasattr(os, 'fchmod') and hasattr(os, 'O_DIRECTORY'), 'POSIX descriptor-relative staging')
    def test_descriptor_relative_stage_without_fchmod_never_chmods_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(books.os, 'fchmod'), patch.object(books.os, 'chmod') as chmod:
                    del books.os.fchmod
                    with self.assertRaisesRegex(OSError, 'requires fchmod'):
                        books._stage_book_stream('stage', stream(b'content'), 0o600, None,
                                                 directory_fd=descriptor)
                    chmod.assert_not_called()
            finally:
                os.close(descriptor)

    @unittest.skipUnless(hasattr(os, 'fchmod') and hasattr(os, 'O_DIRECTORY'), 'POSIX descriptor-relative staging')
    def test_missing_fchmod_cleans_failed_transaction_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'file'
            target.write_bytes(b'original')
            with books.ensure_book_state(root), patch.object(books.os, 'fchmod'):
                del books.os.fchmod
                with self.assertRaisesRegex(OSError, 'requires fchmod'):
                    books.apply_book_state_locked(root, {'file': stream(b'new')}, [], stream_byte_limit=100)
            self.assertEqual(b'original', target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_stream_cleanup_rechecks_parent_before_reading_target(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'
            parent.mkdir()
            (parent / 'file').write_bytes(b'generated')
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parents = [books._capture_target_parent_identities(descriptor, 'sub/file')]
            finally:
                os.close(descriptor)
            data = {'phase': 'committed', 'entries': [{'path': 'sub/file'}],
                    'target_parent_identities': parents, 'stream_limits': {'bytes': 100, 'entries': 10}}
            get_path = books._get_path
            def swap_after_lookup(*args):
                target = get_path(*args)
                parent.rename(outside / 'moved')
                parent.symlink_to(outside / 'moved', target_is_directory=True)
                return target
            with patch.object(books, '_get_path', side_effect=swap_after_lookup), \
                 patch.object(books, '_verify_committed_stream_targets') as verify:
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._verify_stream_cleanup_targets(root, data)
                verify.assert_not_called()
            self.assertEqual(b'generated', (outside / 'moved' / 'file').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX parent identity evidence')
    def test_stream_cleanup_rechecks_parent_after_target_validation(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'
            parent.mkdir()
            (parent / 'file').write_bytes(b'generated')
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parents = [books._capture_target_parent_identities(descriptor, 'sub/file')]
            finally:
                os.close(descriptor)
            data = {'phase': 'committed', 'entries': [{'path': 'sub/file'}],
                    'target_parent_identities': parents, 'stream_limits': {'bytes': 100, 'entries': 10}}
            def swap_during_validation(*args):
                parent.rename(outside / 'moved')
                parent.symlink_to(outside / 'moved', target_is_directory=True)
            with patch.object(books, '_verify_committed_stream_targets', side_effect=swap_during_validation):
                with self.assertRaisesRegex(ValueError, 'parent changed'):
                    books._verify_stream_cleanup_targets(root, data)
            self.assertEqual(b'generated', (outside / 'moved' / 'file').read_bytes())

    def test_stage_finish_observes_flushed_bytes_on_success_and_failure(self):
        class ShortInput(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('read failed')
                return super().read(min(size, 3))

        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'new-0'
                payload = b'abcdef'
                source = (books.BookStateStream(ShortInput(payload), len(payload),
                                               hashlib.sha256(payload).hexdigest())
                          if fails else stream(payload))
                versions = []
                if fails:
                    with self.assertRaisesRegex(OSError, 'read failed'):
                        books._stage_book_stream(path, source, 0o600, None,
                                                 on_finish=versions.append)
                else:
                    books._stage_book_stream(path, source, 0o600, None,
                                             on_finish=versions.append)
                self.assertEqual(1, len(versions))
                self.assertEqual(3 if fails else len(payload), versions[0].st_size)
                self.assertEqual(payload[:versions[0].st_size], path.read_bytes())

    def test_stream_replacement_rechecks_identity_after_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'new-0'
            path.write_bytes(b'new audio')
            info = path.lstat()
            row = {
                'new_file_identity': [info.st_dev, info.st_ino],
                'new_sha256': hashlib.sha256(b'new audio').hexdigest(),
            }
            digest = books._bounded_book_digest
            def replace_after_digest(candidate, budget):
                result = digest(candidate, budget)
                candidate.rename(candidate.with_name('replaced'))
                candidate.write_bytes(b'new audio')
                return result
            budget = books._BookScanBudget(100, 10, None)
            with patch.object(books, '_bounded_book_digest', side_effect=replace_after_digest):
                with self.assertRaisesRegex(ValueError, 'replacement changed'):
                    books._validate_stream_replacement(row, path, budget)

    def test_windows_cleanup_inventory_refuses_reparse_before_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'junction'
            child.mkdir()
            real_lstat = Path.lstat
            class ReparseInfo:
                st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
                def __init__(self, value):
                    self.value = value
                def __getattr__(self, name):
                    return getattr(self.value, name)
            def marked(candidate):
                info = real_lstat(candidate)
                return ReparseInfo(info) if candidate == child else info
            with patch.object(Path, 'lstat', marked):
                with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
                    books._capture_cleanup_tree_path(root)

    def test_stream_transaction_rejects_nested_replacement_and_removal_targets(self):
        for replacement, removal in (('chapter', 'chapter/old.wav'), ('chapter/new.wav', 'chapter')):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = stream(b'new audio')
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'conflicting paths'):
                    books.apply_book_state_locked(root, {replacement: source}, [removal],
                                                  stream_byte_limit=100)
                self.assertEqual(0, source.stream.tell())
                self.assertFalse((root / books.JOURNAL).exists())

    def test_stream_recovery_rejects_journal_with_nested_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / books.JOURNAL
            document = {'version': 2, 'phase': 'publishing', 'workspace': '.book-switch-fixture',
                        'entries': [{'path': 'chapter/new.wav'}, {'path': 'chapter'}],
                        'stream_limits': {'bytes': 100, 'entries': 10}}
            journal.write_text(json.dumps(document), encoding='utf-8')
            before = journal.read_bytes()
            with self.assertRaisesRegex(ValueError, 'conflicting paths'):
                with books.ensure_book_state(root):
                    self.fail('Conflicting recovery targets were admitted')
            self.assertEqual(before, journal.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'directory descriptor semantics require POSIX qualification')
    def test_stream_staging_does_not_follow_replaced_workspace_name(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root, outside = Path(tmp), Path(outside_tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            stage = books._stage_book_stream
            relocated = root / 'retained-workspace'
            swapped = []
            def swap_before_stage(path, source, mode, cancel_check, **kwargs):
                if not swapped:
                    workspace = next(root.glob('.book-switch-*'))
                    workspace.rename(relocated)
                    workspace.symlink_to(outside, target_is_directory=True)
                    swapped.append(workspace)
                return stage(path, source, mode, cancel_check, **kwargs)
            with patch.object(books, '_stage_book_stream', side_effect=swap_before_stage):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new audio')}, [],
                                                  stream_byte_limit=100)
            self.assertTrue(swapped)
            self.assertFalse((outside / 'new-0').exists())
            self.assertEqual(b'new audio', (relocated / 'new-0').read_bytes())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())

    @unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd
                         and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'),
                         'POSIX descriptor-relative move unavailable')
    def test_deep_anchored_move_uses_bounded_descriptors(self):
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 32:
            self.skipTest('descriptor hard limit too low for test')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root
            for _ in range(70):
                directory = directory / 'd'
                directory.mkdir()
            source, destination = directory / 'source', root / 'destination'
            source.write_bytes(b'original')
            code = '''
import resource,sys
from pathlib import Path
import book_state_transaction as b
_,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(32,hard))
b._move(Path(sys.argv[2]),Path(sys.argv[3]),exclusive=True,anchor=Path(sys.argv[1]))
'''
            result = subprocess.run([sys.executable, '-c', code, str(root), str(source), str(destination)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(source.exists())
            self.assertEqual(b'original', destination.read_bytes())

    def test_partial_bytes_staging_failure_cleans_only_owned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            (root / 'state.json').write_bytes(b'old state')
            stage = books._stage_book_stream
            class FailingInput:
                calls = 0
                def read(self, size):
                    self.calls += 1
                    if self.calls == 1:
                        return b'partial'
                    raise OSError('staging failed')
            def fail_after_open(path, source, mode, cancel_check, *, on_open=None, on_finish=None,
                                directory_fd=None):
                if Path(path).name == 'new-1':
                    source = books.BookStateStream(FailingInput(), 10, hashlib.sha256(b'0123456789').hexdigest())
                return stage(path, source, mode, cancel_check, on_open=on_open,
                             on_finish=on_finish, directory_fd=directory_fd)
            with patch.object(books, '_stage_book_stream', side_effect=fail_after_open):
                with books.ensure_book_state(root), self.assertRaisesRegex(OSError, 'staging failed'):
                    books.apply_book_state_locked(root, {'audio.wav':stream(b'new audio'),
                        'state.json':b'new state'}, [], stream_byte_limit=100)
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertEqual(b'old state', (root / 'state.json').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'Windows junction qualification requires a native test')
    def test_stream_cleanup_refuses_parent_swapped_after_publication(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_dir:
            root, outside = Path(temporary), Path(outside_dir)
            parent = root / 'sub'; parent.mkdir()
            target = parent / 'file'; target.write_bytes(b'original')
            move, swapped = books._move, []
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
                    books.apply_book_state_locked(root, {'sub/file': stream(b'generated')}, [],
                                                  stream_byte_limit=100)
            self.assertTrue(swapped)
            self.assertEqual(b'generated', (outside / 'file').read_bytes())
            self.assertEqual(b'original', next(root.glob('.book-switch-*/old-0')).read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'hardlink fixture requires POSIX link semantics')
    def test_hardlinked_replacement_is_preserved_for_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            save, cancelled = books._save_journal, [False]
            def link_after_publish(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing':
                    os.link(root / document['workspace'] / 'new-0', root / 'foreign-link')
                    cancelled[0] = True
                return result
            with patch.object(books, '_save_journal', side_effect=link_after_publish):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'replacement ownership changed'):
                    books.apply_book_state_locked(root, {'audio.wav':stream(b'new audio')}, [],
                                                  stream_byte_limit=100, cancel_check=lambda: cancelled[0])
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(b'new audio', (root / journal['workspace'] / 'new-0').read_bytes())
            self.assertEqual(b'new audio', (root / 'foreign-link').read_bytes())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())

    def test_cancel_during_final_restore_keeps_journal_for_retry(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); move=b._move; count=[0]
def stop(source,destination,**kwargs):
 move(source,destination,**kwargs); count[0]+=1
 if count[0]==2: os._exit(87)
b._move=stop
data=b'new audio'; value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=100)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            result = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(87, result.returncode, result.stderr.decode(errors='replace'))
            moved, cancel = books._move, [False]
            def restore_then_cancel(source, destination, **kwargs):
                moved(source, destination, **kwargs)
                if Path(source).name == 'old-0':
                    cancel[0] = True
            with patch.object(books, '_move', side_effect=restore_then_cancel):
                with self.assertRaises(AudioGenerationCancelled):
                    with books.ensure_book_state(root, cancel_check=lambda: cancel[0]):
                        self.fail('Cancelled recovery was admitted')
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            finish = books._finish_transaction
            cleanup_checks = []
            def observe_cleanup(*args, **kwargs):
                cleanup_checks.append(kwargs.get('cancel_check'))
                return finish(*args, **kwargs)
            deferred_cancel = lambda: False
            with patch.object(books, '_finish_transaction', side_effect=observe_cleanup), \
                 books.ensure_book_state(root, cancel_check=deferred_cancel):
                pass
            self.assertEqual([None], cleanup_checks)
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())

    def test_saved_cleanup_observation_rejects_untrusted_metadata(self):
        original = {'version':1, 'workspace_identity':[1, 2], 'bytes':5, 'entries':[
            {'path':'old-0', 'kind':'directory', 'file_version':[1, 2, 0, 0, 0, stat.S_IFDIR | 0o700, 1]},
            {'path':'old-0/audio.wav', 'kind':'file', 'file_version':[1, 3, 5, 0, 0, stat.S_IFREG | 0o600, 1],
             'sha256':hashlib.sha256(b'audio').hexdigest()}]}
        def encoded(document):
            return json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
        def decode(raw, digest=None):
            return books.decode_book_cleanup_observation(raw, digest or hashlib.sha256(raw).hexdigest(),
                                                         byte_limit=100, entry_limit=20)
        raw = encoded(original)
        self.assertEqual(original, decode(raw))
        with self.assertRaisesRegex(ValueError, 'expected identity'):
            decode(raw, '0' * 64)

        for mutation in ('duplicate', 'missing_parent', 'traversal', 'boolean_size', 'wrong_total', 'huge_integer'):
            value = json.loads(raw)
            if mutation == 'duplicate':
                value['entries'].append(dict(value['entries'][1]))
            elif mutation == 'missing_parent':
                value['entries'].pop(0)
            elif mutation == 'traversal':
                value['entries'][1]['path'] = '../outside'
            elif mutation == 'boolean_size':
                value['entries'][1]['file_version'][2] = True
            elif mutation == 'wrong_total':
                value['bytes'] = 4
            else:
                value['workspace_identity'][0] = 10**100
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                decode(encoded(value))
        duplicate_field = b'{"version":1,"version":1,"workspace_identity":[1,2],"entries":[],"bytes":0}'
        with self.assertRaisesRegex(ValueError, 'JSON'):
            decode(duplicate_field)
        with patch.object(books, 'MAX_BOOK_JOURNAL_BYTES', 32):
            with self.assertRaisesRegex(ValueError, 'bounds'):
                decode(raw)

    def test_cleanup_observation_export_rejects_large_scalars_before_serializing(self):
        observation = {'version': 1, 'workspace_identity': [1, 2], 'bytes': 10000,
                       'entries': [{'path': 'link', 'kind': 'symlink',
                                    'file_version': [1, 2, 10000, 0, 0, stat.S_IFLNK | 0o777, 1],
                                    'target': 'x' * 10000}], 'sha256': '0' * 64}
        for target in ('x' * 10000, '\U0001f600' * 100):
            observation['entries'][0]['target'] = target
            with self.subTest(target_length=len(target)):
                with patch.object(books, 'MAX_BOOK_JOURNAL_BYTES', 512):
                    with patch.object(json.JSONEncoder, 'iterencode', side_effect=AssertionError('serialized')):
                        with self.assertRaises(ValueError):
                            books.encode_book_cleanup_observation(observation, byte_limit=20000, entry_limit=10)

    def test_cleanup_observation_rejects_pathological_json_before_allocating(self):
        raw = b'[' + b'{},' * 1000 + b'{}]'
        with patch.object(books.json, 'loads', side_effect=AssertionError('unbounded JSON parsed')):
            with self.assertRaisesRegex(ValueError, 'structure exceeds its entry limit'):
                books.decode_book_cleanup_observation(raw, hashlib.sha256(raw).hexdigest(),
                                                      byte_limit=100, entry_limit=1)
        deeply_nested = b'[' * 9 + b']' * 9
        with self.assertRaisesRegex(ValueError, 'depth limit'):
            books.decode_book_cleanup_observation(deeply_nested, hashlib.sha256(deeply_nested).hexdigest(),
                                                  byte_limit=100, entry_limit=10)
        large = b'[' + b'0,' * 40000 + b'0]'
        checks = [0]
        def cancel():
            checks[0] += 1
            return checks[0] >= 3
        with self.assertRaises(AudioGenerationCancelled):
            books.decode_book_cleanup_observation(large, hashlib.sha256(large).hexdigest(),
                byte_limit=100, entry_limit=8192, cancel_check=cancel)

    def test_empty_cleanup_observation_honors_late_cancellation(self):
        raw = b'{"version":1,"workspace_identity":[1,2],"entries":[],"bytes":0}'
        checks = [0]
        def cancel():
            checks[0] += 1
            return checks[0] >= 3
        with self.assertRaises(AudioGenerationCancelled):
            books.decode_book_cleanup_observation(raw, hashlib.sha256(raw).hexdigest(),
                byte_limit=100, entry_limit=10, cancel_check=cancel)

    def test_cleanup_observation_rejects_digest_string_subclass(self):
        class Forged(str):
            def __iter__(self):
                return iter('0' * 64)
            def __ne__(self, other):
                return False
        raw = b'{"version":1,"workspace_identity":[1,2],"entries":[],"bytes":0}'
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual([], books.decode_book_cleanup_observation(
            raw, digest, byte_limit=100, entry_limit=10)['entries'])
        with self.assertRaisesRegex(ValueError, 'bounds or identity'):
            books.decode_book_cleanup_observation(raw, Forged(digest), byte_limit=100, entry_limit=10)

    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_is_bounded_read_only_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary).resolve()
            workspace = root / '.book-switch-test'
            workspace.mkdir()
            (workspace / 'old-0').mkdir()
            (workspace / 'old-0' / 'audio.wav').write_bytes(b'audio')
            (workspace / 'old-0' / 'empty').mkdir()
            other = Path(outside).resolve() / 'private'
            other.write_bytes(b'private bytes')
            if os.name != 'nt':
                (workspace / 'link').symlink_to(other)
            observed = books.inspect_book_cleanup_workspace(workspace, byte_limit=10000, entry_limit=20)
            entries = {row['path']: row for row in observed['entries']}
            self.assertEqual('directory', entries['old-0/empty']['kind'])
            self.assertEqual(hashlib.sha256(b'audio').hexdigest(), entries['old-0/audio.wav']['sha256'])
            if os.name != 'nt':
                self.assertEqual('symlink', entries['link']['kind'])
                self.assertEqual(str(other), entries['link']['target'])
            self.assertEqual(b'private bytes', other.read_bytes())
            self.assertEqual(observed['sha256'], books.inspect_book_cleanup_workspace(
                workspace, byte_limit=10000, entry_limit=20)['sha256'])
            encoded = books.encode_book_cleanup_observation(observed, byte_limit=10000, entry_limit=20)
            decoded = books.decode_book_cleanup_observation(encoded, observed['sha256'],
                                                             byte_limit=10000, entry_limit=20)
            self.assertEqual(observed['entries'], decoded['entries'])
            altered = {**observed, 'bytes': observed['bytes'] + 1}
            with self.assertRaisesRegex(ValueError, 'expected identity'):
                books.encode_book_cleanup_observation(altered, byte_limit=10000, entry_limit=20)
            with self.assertRaisesRegex(ValueError, 'entry limit'):
                books.inspect_book_cleanup_workspace(workspace, byte_limit=10000, entry_limit=1)
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                books.inspect_book_cleanup_workspace(workspace, byte_limit=1, entry_limit=20)
            with self.assertRaises(AudioGenerationCancelled):
                books.inspect_book_cleanup_workspace(workspace, byte_limit=10000, entry_limit=20,
                                                     cancel_check=lambda: True)
            self.assertEqual(b'audio', (workspace / 'old-0' / 'audio.wav').read_bytes())

    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_refuses_file_changed_during_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            target = workspace / 'old-0'
            target.write_bytes(b'original')
            original_stat = books._cleanup_observation_stat
            changed = [False]
            def mutate(path, directory_fd):
                result = original_stat(path, directory_fd)
                if path == 'old-0' and not changed[0]:
                    changed[0] = True
                    target.write_bytes(b'changed')
                return result
            with patch.object(books, '_cleanup_observation_stat', side_effect=mutate):
                with self.assertRaisesRegex(ValueError, 'changed during observation'):
                    books.inspect_book_cleanup_workspace(workspace, byte_limit=100, entry_limit=20)
            self.assertEqual(b'changed', target.read_bytes())

    @unittest.skipUnless(os.scandir in os.supports_fd and os.readlink in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_rechecks_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'link').symlink_to('aaaa')
            actual = os.readlink
            calls = []
            def changed(name, *args, **kwargs):
                result = actual(name, *args, **kwargs)
                if name == 'link':
                    calls.append(True)
                    return 'bbbb' if len(calls) == 1 else result
                return result
            # Preserve the platform capability check while substituting only
            # the readlink behavior used by the scan.
            with patch.object(books.os, 'readlink', side_effect=changed) as readlink:
                with patch.object(books.os, 'supports_dir_fd', books.os.supports_dir_fd | {readlink}):
                    with self.assertRaisesRegex(ValueError, 'symlink changed'):
                        books.inspect_book_cleanup_workspace(root, byte_limit=100, entry_limit=10)

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'O_NOFOLLOW unavailable')
    def test_cleanup_observation_refuses_zero_nofollow_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(books.os, 'O_NOFOLLOW', 0):
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    books.inspect_book_cleanup_workspace(Path(temporary), byte_limit=100, entry_limit=10)

    @unittest.skipUnless(os.scandir in os.supports_fd and os.stat in os.supports_follow_symlinks
                         and os.readlink in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_comparison_describes_partial_deletion_without_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old = root / 'old-0'
            old.mkdir()
            (old / 'removed').write_bytes(b'one')
            (old / 'changed').write_bytes(b'old')
            (old / 'unchanged').write_bytes(b'keep')
            before = books.inspect_book_cleanup_workspace(root, byte_limit=1000, entry_limit=20)
            first = books.encode_book_cleanup_observation(before, byte_limit=1000, entry_limit=20)
            (old / 'removed').unlink()
            (old / 'changed').write_bytes(b'newer')
            (old / 'foreign').write_bytes(b'user data')
            after = books.inspect_book_cleanup_workspace(root, byte_limit=1000, entry_limit=20)
            second = books.encode_book_cleanup_observation(after, byte_limit=1000, entry_limit=20)
            result = books.compare_book_cleanup_observations(first, before['sha256'], second,
                after['sha256'], byte_limit=1000, entry_limit=20)
            self.assertEqual(['old-0', 'old-0/unchanged'], result['matching'])
            self.assertEqual(['old-0/removed'], result['absent'])
            self.assertEqual(['old-0/changed'], result['changed'])
            self.assertEqual(['old-0/foreign'], result['unexpected'])
            self.assertEqual(b'user data', (old / 'foreign').read_bytes())
            with self.assertRaises(AudioGenerationCancelled):
                books.compare_book_cleanup_observations(first, before['sha256'], second,
                    after['sha256'], byte_limit=1000, entry_limit=20, cancel_check=lambda: True)
            with tempfile.TemporaryDirectory() as other:
                different = books.inspect_book_cleanup_workspace(Path(other).resolve(),
                                                                   byte_limit=1000, entry_limit=20)
                third = books.encode_book_cleanup_observation(different, byte_limit=1000, entry_limit=20)
                with self.assertRaisesRegex(ValueError, 'workspace identity differs'):
                    books.compare_book_cleanup_observations(first, before['sha256'], third,
                        different['sha256'], byte_limit=1000, entry_limit=20)
    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_refuses_a_swapped_parent(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root, other = Path(temporary).resolve(), Path(outside).resolve()
            directory = root / 'inner'
            directory.mkdir()
            (directory / 'original').write_bytes(b'original')
            (other / 'secret').write_bytes(b'private')
            open_file = books._cleanup_observation_open
            swapped, visited = [False], []
            def swap(path, flags, directory_fd):
                visited.append(str(path))
                if path == 'inner' and not swapped[0]:
                    directory.rename(root / 'retained')
                    directory.symlink_to(other, target_is_directory=True)
                    swapped[0] = True
                return open_file(path, flags, directory_fd)
            with patch.object(books, '_cleanup_observation_open', side_effect=swap):
                with self.assertRaises((ValueError, OSError)):
                    books.inspect_book_cleanup_workspace(root, byte_limit=1000, entry_limit=20)
            self.assertTrue(swapped[0])
            self.assertFalse(any('secret' in Path(path).parts for path in visited))
            self.assertEqual(b'private', (other / 'secret').read_bytes())

    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_refuses_swapped_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            base = Path(temporary).resolve() / 'base'
            base.mkdir()
            workspace = base / 'workspace'
            workspace.mkdir()
            (workspace / 'original').write_bytes(b'original')
            other = Path(outside).resolve()
            (other / 'workspace').mkdir()
            (other / 'workspace' / 'secret').write_bytes(b'private')
            open_file = books._cleanup_observation_open
            swapped, visited = [False], []
            def swap(path, flags, directory_fd):
                visited.append(str(path))
                if path == 'base' and not swapped[0]:
                    base.rename(base.with_name('retained'))
                    base.symlink_to(other, target_is_directory=True)
                    swapped[0] = True
                return open_file(path, flags, directory_fd)
            with patch.object(books, '_cleanup_observation_open', side_effect=swap):
                with self.assertRaises((ValueError, OSError)):
                    books.inspect_book_cleanup_workspace(workspace, byte_limit=1000, entry_limit=20)
            self.assertTrue(swapped[0])
            self.assertFalse(any('secret' in Path(path).parts for path in visited))
            self.assertEqual(b'private', (other / 'workspace' / 'secret').read_bytes())

    def test_cleanup_observation_rejects_duplicate_directory_names(self):
        from book_recovery_archive import _validate_portable_inventory_names
        with self.assertRaisesRegex(ValueError, 'duplicate paths'):
            _validate_portable_inventory_names([], ['directory', 'directory'])
        with self.assertRaisesRegex(ValueError, 'duplicate paths'):
            _validate_portable_inventory_names(['directory'], ['directory'])

    @unittest.skipUnless(os.scandir in os.supports_fd and hasattr(os, 'mkfifo'),
                         'descriptor-relative POSIX observation unavailable')
    def test_cleanup_observation_never_opens_fifo_and_bounds_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            fifo = workspace / 'fifo'
            os.mkfifo(fifo)
            with patch.object(books, '_cleanup_observation_open', wraps=books._cleanup_observation_open) as opened:
                with self.assertRaisesRegex(ValueError, 'unsupported object'):
                    books.inspect_book_cleanup_workspace(workspace, byte_limit=1000, entry_limit=20)
            names = [call.args[0] if call.args else call.kwargs.get('name')
                     for call in opened.call_args_list]
            self.assertFalse(any(name is not None and Path(os.fsdecode(name)).name == 'fifo'
                                 for name in names))
            self.assertTrue(fifo.exists())
            fifo.unlink()
            (workspace / 'old-0').write_bytes(b'original')
            with patch.object(books, 'MAX_BOOK_JOURNAL_BYTES', 32):
                with self.assertRaisesRegex(ValueError, 'metadata limit'):
                    books.inspect_book_cleanup_workspace(workspace, byte_limit=1000, entry_limit=20)
            self.assertEqual(b'original', (workspace / 'old-0').read_bytes())

    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_cleanup_observation_refuses_nonportable_directory_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'Foo').mkdir()
            try:
                (root / 'foo').mkdir()
            except FileExistsError:
                self.skipTest('case-insensitive filesystem')
            (root / 'Foo' / 'first').write_bytes(b'one')
            (root / 'foo' / 'second').write_bytes(b'two')
            with self.assertRaisesRegex(ValueError, 'conflicting portable paths'):
                books.inspect_book_cleanup_workspace(root, byte_limit=1000, entry_limit=20)
            self.assertEqual(b'one', (root / 'Foo' / 'first').read_bytes())
            self.assertEqual(b'two', (root / 'foo' / 'second').read_bytes())

    @unittest.skipUnless(os.scandir in os.supports_fd and os.open in os.supports_dir_fd,
                         'descriptor-relative cleanup observation unavailable')
    def test_deep_cleanup_observation_uses_bounded_descriptors(self):
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 32:
            self.skipTest('descriptor hard limit too low for test')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root
            for _ in range(70):
                directory = directory / 'd'
                directory.mkdir()
            (directory / 'file').write_bytes(b'content')
            code = '''
import resource,sys
import book_state_transaction as b
import book_recovery_archive
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(32,hard))
result=b.inspect_book_cleanup_workspace(sys.argv[1],byte_limit=1000,entry_limit=100)
print(len(result['entries']))
'''
            result = subprocess.run([sys.executable, '-c', code, str(root)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('71', result.stdout.strip())

    def test_interrupted_directory_cleanup_preserves_journal_and_remaining_backup(self):
        # This documents an unresolved recovery case, not successful cleanup:
        # a partly deleted committed backup requires inspection on the next run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            directory = root / 'chapters'
            directory.mkdir()
            (directory / 'first').write_bytes(b'one')
            (directory / 'second').write_bytes(b'two')
            if os.name == 'nt':
                remove = books._remove_windows_owned_tree
                def interrupted(path, *_args, **_kwargs):
                    if Path(path).name != 'old-1':
                        return remove(path, *_args, **_kwargs)
                    (Path(path) / 'first').unlink()
                    raise OSError('simulated interrupted cleanup')
                cleanup = patch.object(books, '_remove_windows_owned_tree',
                                       side_effect=interrupted)
            else:
                def interrupted(parent_fd, name, *_args, **_kwargs):
                    self.assertEqual('old-1', name)
                    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY,
                                         dir_fd=parent_fd)
                    try:
                        os.unlink('first', dir_fd=descriptor)
                    finally:
                        os.close(descriptor)
                    raise OSError('simulated interrupted cleanup')
                cleanup = patch.object(books, '_remove_owned_tree_at',
                                       side_effect=interrupted)
            with cleanup:
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'audio.wav':stream(b'new audio')},
                                                  ['chapters'], stream_byte_limit=100)
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual('committed', journal['phase'])
            self.assertEqual(b'two', (root / journal['workspace'] / 'old-1' / 'second').read_bytes())
            self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())
            with self.assertRaises(ValueError):
                with books.ensure_book_state(root):
                    self.fail('Partial backup was silently accepted')

    @unittest.skipIf(os.name == 'nt', 'symlink semantics require POSIX qualification')
    def test_stream_cleanup_refuses_workspace_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root, outside = Path(tmp), Path(outside_tmp)
            directory = root / 'chapters'
            directory.mkdir()
            (directory / 'owned').write_bytes(b'owned')
            validate = books._validate_stream_workspace_for_cleanup
            moved = []
            def swap_after_validation(workspace, document, cancel_check=None):
                result = validate(workspace, document, cancel_check=cancel_check)
                relocated = outside / 'workspace'
                Path(workspace).rename(relocated)
                Path(workspace).symlink_to(relocated, target_is_directory=True)
                moved.append(relocated)
                return result
            source = stream(b'generated')
            with patch.object(books, '_validate_stream_workspace_for_cleanup',
                              side_effect=swap_after_validation):
                with books.ensure_book_state(root), self.assertRaises((OSError, ValueError)):
                    books.apply_book_state_locked(root, {'script.json': source}, ['chapters'],
                                                  stream_byte_limit=1024)
            self.assertEqual(1, len(moved))
            self.assertEqual(b'owned', (moved[0] / 'old-1' / 'owned').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    def test_unexpected_prepublication_file_is_preserved_on_staging_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            class BrokenInput:
                def read(self, _size):
                    workspace = next(root.glob('.book-switch-*'))
                    (workspace / 'foreign').write_bytes(b'user data')
                    raise OSError('input failed')
            value = books.BookStateStream(BrokenInput(), 1, hashlib.sha256(b'x').hexdigest())
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'unowned data'):
                books.apply_book_state_locked(root, {'audio.wav': value}, [], stream_byte_limit=100)
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(b'user data', (workspace / 'foreign').read_bytes())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_file_added_during_prepublication_cleanup_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            class BrokenInput:
                def read(self, _size):
                    raise OSError('input failed')
            value = books.BookStateStream(BrokenInput(), 1, hashlib.sha256(b'x').hexdigest())
            unlink = books._unlink_owned_staging_file
            def add_foreign(workspace, name, directory_fd):
                (workspace / 'foreign').write_bytes(b'user data')
                unlink(workspace, name, directory_fd)
            with patch.object(books, '_unlink_owned_staging_file', side_effect=add_foreign):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'unowned data'):
                    books.apply_book_state_locked(root, {'audio.wav':value}, [], stream_byte_limit=100)
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(b'user data', (workspace / 'foreign').read_bytes())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_file_added_after_committed_cleanup_validation_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            validate = books._validate_stream_workspace_for_cleanup
            injected = []
            def inject_after_validation(workspace, data, cancel_check=None):
                owned = validate(workspace, data, cancel_check=cancel_check)
                if data['phase'] == 'committed' and not injected:
                    foreign = workspace / 'foreign'
                    foreign.write_bytes(b'user data')
                    injected.append(foreign)
                return owned
            with patch.object(books, '_validate_stream_workspace_for_cleanup',
                              side_effect=inject_after_validation):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'unowned data'):
                    books.apply_book_state_locked(root, {'audio.wav':stream(b'new audio')}, [],
                                                  stream_byte_limit=100)
            self.assertEqual(b'user data', injected[0].read_bytes())
            self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'Windows does not allow replacing the open staging file')
    def test_replaced_prepublication_staging_file_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            class ReplacedInput:
                def read(self, _size):
                    stage = next(root.glob('.book-switch-*')) / 'new-0'
                    stage.unlink()
                    stage.write_bytes(b'foreign replacement')
                    raise OSError('input failed')
            value = books.BookStateStream(ReplacedInput(), 1, hashlib.sha256(b'x').hexdigest())
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'unowned data'):
                books.apply_book_state_locked(root, {'audio.wav': value}, [], stream_byte_limit=100)
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(b'foreign replacement', (workspace / 'new-0').read_bytes())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())

    def test_unknown_workspace_file_blocks_cleanup_without_losing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            save = books._save_journal
            def add_foreign(root_path, data, **kwargs):
                result = save(root_path, data, **kwargs)
                if data['phase'] == 'committed':
                    (root / data['workspace'] / 'foreign').write_bytes(b'user data')
                return result
            with patch.object(books, '_save_journal', side_effect=add_foreign):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'unowned data'):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new audio')}, [],
                                                  stream_byte_limit=100)
            journal = json.loads((root / books.JOURNAL).read_text())
            workspace = root / journal['workspace']
            self.assertEqual('committed', journal['phase'])
            self.assertEqual(b'user data', (workspace / 'foreign').read_bytes())
            self.assertEqual(b'old audio', (workspace / 'old-0').read_bytes())
            self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())
            with self.assertRaisesRegex(ValueError, 'unowned data'):
                with books.ensure_book_state(root):
                    self.fail('Unknown data was not preserved')
            (workspace / 'foreign').unlink()
            with books.ensure_book_state(root):
                pass
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())

    def test_bounded_directory_digest_matches_existing_transaction_digest(self):
        from adapter_publication import get_adapter_bundle_sha256
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'chapters'
            directory.mkdir()
            (directory / 'first.wav').write_bytes(b'chapter one')
            (directory / 'nested').mkdir()
            (directory / 'nested/second.wav').write_bytes(b'chapter two')
            try:
                (directory / 'linked-directory').symlink_to('nested', target_is_directory=True)
                (directory / 'linked-file').symlink_to('first.wav')
                (directory / 'broken-link').symlink_to('missing')
                (directory / 'loop-link').symlink_to('loop-link')
            except (OSError, NotImplementedError):
                pass
            budget = books._BookScanBudget(10000, 100, None)
            self.assertEqual(get_adapter_bundle_sha256(str(directory)),
                             books._bounded_book_digest(directory, budget))

    def test_adapter_bundle_digest_propagates_walk_errors(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            def denied(_root, onerror=None):
                onerror(PermissionError('fixture walk refusal'))
                return ()
            with patch.object(adapter_publication.os, 'walk', side_effect=denied):
                with self.assertRaisesRegex(PermissionError, 'fixture walk refusal'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)

    def test_adapter_bundle_digest_rejects_empty_root(self):
        import adapter_publication
        with patch.object(adapter_publication.os, 'walk', return_value=()) as walk:
            with self.assertRaisesRegex(ValueError, 'root is required'):
                adapter_publication.get_adapter_bundle_sha256('')
        walk.assert_not_called()

    def test_adapter_bundle_digest_rejects_invalid_root_type(self):
        import adapter_publication
        for root in (None, b'fixture'):
            with self.subTest(root=root), self.assertRaisesRegex(
                    ValueError, 'root is required'):
                adapter_publication.get_adapter_bundle_sha256(root)

    @unittest.skipIf(os.name == 'nt', 'POSIX surrogate-escaped filename required')
    def test_adapter_bundle_digest_accepts_surrogate_escaped_filename(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            name = os.fsdecode(b'adapter-\xff')
            try:
                (Path(temporary) / name).write_bytes(b'owned')
            except OSError as error:
                if error.errno != errno.EILSEQ:
                    raise
                self.skipTest('this filesystem refuses non-UTF-8 names')
            digest = adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
            self.assertEqual(
                books._bounded_book_digest(
                    Path(temporary), books._BookScanBudget(1024, 8, None)),
                digest)

    def test_adapter_bundle_digest_preserves_non_ascii_encoding(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'café'
            target.write_bytes(b'owned')
            rows = [
                ('.', 'directory', stat.S_IMODE(root.lstat().st_mode)),
                ('café', 'file', stat.S_IMODE(target.lstat().st_mode),
                 hashlib.sha256(b'owned').hexdigest()),
            ]
            expected = hashlib.sha256(
                json.dumps(rows, ensure_ascii=False).encode('utf-8')).hexdigest()
            self.assertEqual(
                expected, adapter_publication.get_adapter_bundle_sha256(temporary))

    def test_adapter_bundle_digest_refuses_late_ancestor_change(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'child'
            child.mkdir()
            (child / 'artifact').write_bytes(b'owned')
            real_lstat = adapter_publication.os.lstat
            root_before = real_lstat(root)
            checks = []
            def mutate_during_child_scan():
                checks.append(True)
                if len(checks) == 5:
                    (root / 'late').write_bytes(b'foreign')
                return False
            def expose_late_root_version(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path) == root and (root / 'late').exists():
                    return SimpleNamespace(
                        st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                        st_mtime_ns=root_before.st_mtime_ns + 1,
                        st_ctime_ns=info.st_ctime_ns, st_mode=info.st_mode,
                        st_nlink=info.st_nlink)
                return info
            with patch.object(adapter_publication.os, 'lstat',
                              side_effect=expose_late_root_version):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adapter_publication.get_adapter_bundle_sha256(
                        temporary, cancel_check=mutate_during_child_scan)
            self.assertTrue((root / 'late').exists())

    def test_adapter_bundle_digest_refuses_file_changed_during_finalization(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            dumps = adapter_publication.json.dumps
            def mutate_before_return(*args, **kwargs):
                target.write_bytes(b'foreign')
                return dumps(*args, **kwargs)
            with patch.object(adapter_publication.json, 'dumps', side_effect=mutate_before_return):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertEqual(b'foreign', target.read_bytes())

    def test_adapter_bundle_final_recheck_honors_cancellation(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            dumps = adapter_publication.json.dumps
            finalizing = []
            def mark_finalizing(*args, **kwargs):
                finalizing.append(True)
                return dumps(*args, **kwargs)
            with patch.object(adapter_publication.json, 'dumps', side_effect=mark_finalizing):
                with self.assertRaises(AudioGenerationCancelled):
                    adapter_publication.get_adapter_bundle_sha256(
                        temporary, cancel_check=lambda: bool(finalizing))

    def test_adapter_bundle_refuses_child_change_before_descending(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'child'
            child.mkdir()
            target = child / 'artifact'
            target.write_bytes(b'owned')
            real_lstat = adapter_publication.os.lstat
            observed = []
            def stabilize_child_after_parent_observation(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path) != child:
                    return info
                if not observed:
                    observed.append(info)
                    (child / 'late').write_bytes(b'stable')
                    return info
                info = SimpleNamespace(
                    st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                    st_mtime_ns=observed[0].st_mtime_ns + 1,
                    st_ctime_ns=info.st_ctime_ns, st_mode=info.st_mode,
                    st_nlink=info.st_nlink)
                return info
            with patch.object(adapter_publication.os, 'lstat',
                              side_effect=stabilize_child_after_parent_observation):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertTrue(observed)

    def test_adapter_bundle_anchors_relative_root_before_finalization(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            root = temporary / 'bundle'
            other = temporary / 'other'
            root.mkdir()
            other.mkdir()
            (root / 'artifact').write_bytes(b'owned')
            expected = adapter_publication.get_adapter_bundle_sha256(str(root))
            dumps = adapter_publication.json.dumps
            original_cwd = Path.cwd()
            try:
                os.chdir(temporary)
                def change_cwd_before_recheck(*args, **kwargs):
                    os.chdir(other)
                    return dumps(*args, **kwargs)
                with patch.object(adapter_publication.json, 'dumps',
                                  side_effect=change_cwd_before_recheck):
                    actual = adapter_publication.get_adapter_bundle_sha256('bundle')
            finally:
                os.chdir(original_cwd)
            self.assertEqual(expected, actual)

    def test_adapter_bundle_checks_cancellation_after_final_lstat(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            real_lstat = adapter_publication.os.lstat
            dumps = adapter_publication.json.dumps
            finalizing = []
            cancelled = []
            def mark_finalizing(*args, **kwargs):
                finalizing.append(True)
                return dumps(*args, **kwargs)
            def cancel_after_final_lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if finalizing and Path(path) == target:
                    cancelled.append(True)
                return info
            with patch.object(adapter_publication.json, 'dumps',
                              side_effect=mark_finalizing), \
                 patch.object(adapter_publication.os, 'lstat',
                              side_effect=cancel_after_final_lstat):
                with self.assertRaises(AudioGenerationCancelled):
                    adapter_publication.get_adapter_bundle_sha256(
                        temporary, cancel_check=lambda: bool(cancelled))
            self.assertTrue(finalizing)
            self.assertTrue(cancelled)

    def test_regular_byte_digest_refuses_entry_replaced_during_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            retained = Path(temporary) / 'retained'
            target.write_bytes(b'owned')
            real_open = books.os.open
            swapped = []
            def replace_on_open(path, flags, *args, **kwargs):
                if Path(path) == target and not swapped:
                    target.rename(retained)
                    target.write_bytes(b'foreign')
                    swapped.append(True)
                return real_open(path, flags, *args, **kwargs)
            with patch.object(books.os, 'open', side_effect=replace_on_open):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    books._get_digest(target)
            self.assertEqual([True], swapped)
            self.assertEqual(b'owned', retained.read_bytes())
            self.assertEqual(b'foreign', target.read_bytes())

    def test_regular_byte_digest_normalizes_missing_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'missing'
            with self.assertRaisesRegex(ValueError, 'changed'):
                books._get_digest(target)

    def test_directory_digest_refuses_root_replaced_after_bundle_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'bundle'
            retained = Path(temporary) / 'retained'
            target.mkdir()
            (target / 'artifact').write_bytes(b'owned')
            def replace_after_scan(_path, **_kwargs):
                target.rename(retained)
                target.mkdir()
                (target / 'artifact').write_bytes(b'foreign')
                return hashlib.sha256(b'owned digest').hexdigest()
            with patch.object(books, 'get_adapter_bundle_sha256', side_effect=replace_after_scan):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    books._get_digest(target)
            self.assertEqual(b'owned', (retained / 'artifact').read_bytes())
            self.assertEqual(b'foreign', (target / 'artifact').read_bytes())

    def test_adapter_digest_opens_regular_files_nonblocking(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            real_open = adapter_publication.os.open
            sentinel = 1 << 29
            observed = []
            def require_nonblocking(path, flags, *args, **kwargs):
                observed.append(bool(flags & sentinel))
                return real_open(path, flags & ~sentinel, *args, **kwargs)
            with patch.object(adapter_publication.os, 'O_NONBLOCK', sentinel, create=True), \
                 patch.object(adapter_publication.os, 'open', side_effect=require_nonblocking):
                adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertEqual([True], observed)

    def test_adapter_digest_closes_descriptor_when_fdopen_fails(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            real_open = adapter_publication.os.open
            descriptors = []
            def capture_open(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor
            with patch.object(adapter_publication.os, 'open', side_effect=capture_open), \
                 patch.object(adapter_publication.os, 'fdopen', side_effect=RuntimeError('fixture fdopen failure')):
                with self.assertRaisesRegex(RuntimeError, 'fixture fdopen failure'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertEqual(1, len(descriptors))
            try:
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])
            finally:
                try:
                    os.close(descriptors[0])
                except OSError:
                    pass

    def test_adapter_digest_refuses_file_changed_while_reading(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'artifact'
            target.write_bytes(b'owned')
            opened = target.stat()
            changed = type('ChangedStat', (), {
                'st_dev': opened.st_dev, 'st_ino': opened.st_ino,
                'st_size': opened.st_size, 'st_mtime_ns': opened.st_mtime_ns + 1,
                'st_ctime_ns': opened.st_ctime_ns, 'st_mode': opened.st_mode,
                'st_nlink': opened.st_nlink,
            })()
            with patch.object(adapter_publication.os, 'fstat', side_effect=(opened, changed)):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture')
    def test_adapter_digest_refuses_symlink_changed_while_reading(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'first').write_bytes(b'first')
            (root / 'second').write_bytes(b'second')
            link = root / 'link'
            retained = root / 'retained-link'
            link.symlink_to('first')
            real_readlink = adapter_publication.os.readlink
            swapped = []
            def replace_link(path):
                if Path(path) == link and not swapped:
                    link.rename(retained)
                    link.symlink_to('second')
                    swapped.append(True)
                return real_readlink(path)
            with patch.object(adapter_publication.os, 'readlink', side_effect=replace_link):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertEqual([True], swapped)
            self.assertEqual('first', os.readlink(retained))
            self.assertEqual('second', os.readlink(link))

    def test_adapter_digest_refuses_directory_changed_during_walk(self):
        import adapter_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'artifact').write_bytes(b'owned')
            real_lstat = adapter_publication.os.lstat
            original = real_lstat(root)
            changed = type('ChangedStat', (), {
                'st_dev': original.st_dev, 'st_ino': original.st_ino,
                'st_size': original.st_size, 'st_mtime_ns': original.st_mtime_ns + 1,
                'st_ctime_ns': original.st_ctime_ns, 'st_mode': original.st_mode,
                'st_nlink': original.st_nlink,
            })()
            root_calls = []
            def drift_root(path, *args, **kwargs):
                result = real_lstat(path, *args, **kwargs)
                if Path(path) == root:
                    root_calls.append(True)
                    if len(root_calls) == 2:
                        return changed
                return result
            with patch.object(adapter_publication.os, 'lstat', side_effect=drift_root):
                with self.assertRaisesRegex(ValueError, 'directory changed'):
                    adapter_publication.get_adapter_bundle_sha256(temporary)
            self.assertEqual(2, len(root_calls))

    def test_windows_cleanup_child_processing_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                (root / str(index)).write_bytes(b'owned')
            checks = []
            def cancel_after_collection():
                checks.append(True)
                return len(checks) == 5
            with self.assertRaises(AudioGenerationCancelled):
                books._capture_cleanup_tree_path(root, cancel_check=cancel_after_collection)
            self.assertEqual(5, len(checks))

    def test_existing_destinations_obey_aggregate_byte_and_entry_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old' * 1000)
            source = stream(b'new')
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'destination scan.*byte limit'):
                books.apply_book_state_locked(root, {'audio.wav': source}, [], stream_byte_limit=100)
            self.assertEqual(0, source.stream.tell())
            self.assertEqual(b'old' * 1000, (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            directory = root / 'chapters'
            directory.mkdir()
            for index in range(6):
                (directory / str(index)).write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'entry limit'):
                books._bounded_book_digest(directory, books._BookScanBudget(1000, 4, None))
            self.assertEqual(6, len(list(directory.iterdir())))

    def test_transaction_entry_limit_is_checked_before_any_staging(self):
        import book_recovery_archive
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacements = {f'file-{index}': stream(b'x') for index in range(5)}
            with patch.object(book_recovery_archive, 'MAX_FILES', 4):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'entry limit'):
                    books.apply_book_state_locked(root, replacements, [], stream_byte_limit=100)
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))
            self.assertFalse(any(path.exists() for path in (root / name for name in replacements)))

    def test_destination_scan_honors_cancellation_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b'x' * (2 * 1024 * 1024)
            (root / 'audio.wav').write_bytes(original)
            checks = [0]
            def cancel():
                checks[0] += 1
                return checks[0] >= 5
            with books.ensure_book_state(root), self.assertRaises(AudioGenerationCancelled):
                books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [],
                                              stream_byte_limit=len(original), cancel_check=cancel)
            self.assertEqual(original, (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_apply_defers_cancellation_after_commit_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old')
            save = books._save_journal
            committed = []
            def mark_commit(*args, **kwargs):
                result = save(*args, **kwargs)
                data = args[1]
                if data.get('phase') == 'committed':
                    committed.append(True)
                return result
            with patch.object(books, '_save_journal', side_effect=mark_commit), \
                 books.ensure_book_state(root):
                books.apply_book_state_locked(
                    root, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100,
                    cancel_check=lambda: bool(committed))
            self.assertEqual([True], committed)
            self.assertEqual(b'new', (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_published_byte_cleanup_failure_remains_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old')
            save = books._save_journal
            published = []
            validate = books._validate_staged_replacement
            def mark_published(*args, **kwargs):
                result = save(*args, **kwargs)
                published.append(True)
                return result
            def fail_after_publication(*args, **kwargs):
                if published:
                    raise RuntimeError('fixture publication failure')
                return validate(*args, **kwargs)
            with patch.object(books, '_save_journal', side_effect=mark_published), \
                 patch.object(books, '_validate_staged_replacement', side_effect=fail_after_publication), \
                 patch.object(books, '_unlink_owned_book_journal',
                              side_effect=ValueError('fixture cleanup failure')), \
                 books.ensure_book_state(root), \
                 self.assertRaisesRegex(ValueError, 'cleanup failure') as raised:
                books.apply_book_state_locked(root, {'audio.wav': b'new'}, [])
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertEqual(b'old', (root / 'audio.wav').read_bytes())

    def test_large_input_is_staged_in_chunks_and_committed_with_metadata(self):
        class Observed(io.BytesIO):
            def __init__(self, value):
                super().__init__(value)
                self.requests = []
            def read(self, count=-1):
                self.requests.append(count)
                return super().read(count)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            if os.name != 'nt':
                (root / 'audio.wav').chmod(0o640)
            (root / 'state.json').write_bytes(b'old state')
            data = b'synthetic bytes' * 200_000
            source = Observed(data)
            value = books.BookStateStream(source, len(data), hashlib.sha256(data).hexdigest())
            with books.ensure_book_state(root):
                books.apply_book_state_locked(root, {'audio.wav': value, 'state.json': b'new state'}, [],
                                              stream_byte_limit=len(data) + len(b'new state'))
            self.assertEqual(data, (root / 'audio.wav').read_bytes())
            if os.name != 'nt':
                self.assertEqual(0o640, (root / 'audio.wav').stat().st_mode & 0o777)
            self.assertEqual(b'new state', (root / 'state.json').read_bytes())
            self.assertGreater(len(source.requests), 2)
            self.assertLessEqual(max(source.requests), 1024 * 1024)
            self.assertFalse((root / books.JOURNAL).exists())

    def test_wrong_size_hash_or_limit_preserves_existing_book(self):
        data = b'new audio'
        candidates = [
            (books.BookStateStream(io.BytesIO(data), len(data) + 1, hashlib.sha256(data).hexdigest()), 100),
            (books.BookStateStream(io.BytesIO(data), len(data) - 1, hashlib.sha256(data).hexdigest()), 100),
            (books.BookStateStream(io.BytesIO(data), len(data), '0' * 64), 100),
            (stream(data), len(data) - 1),
            (stream(data), None),
        ]
        for value, limit in candidates:
            with self.subTest(size=value.size, limit=limit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'audio.wav').write_bytes(b'old audio')
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'audio.wav': value}, [], stream_byte_limit=limit)
                self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertFalse(list(root.glob('.book-switch-*')))

    def test_cancellation_during_staging_preserves_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            checks = [0]
            def cancel():
                checks[0] += 1
                return checks[0] >= 3
            data = b'x' * (2 * 1024 * 1024)
            with books.ensure_book_state(root), self.assertRaises(AudioGenerationCancelled):
                books.apply_book_state_locked(root, {'audio.wav': stream(data)}, [],
                    stream_byte_limit=len(data), cancel_check=cancel)
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    def test_destination_edit_during_staging_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            class EditOnRead(io.BytesIO):
                def read(self, count=-1):
                    target.write_bytes(b'concurrent edit')
                    return super().read(count)
            data = b'new audio'
            value = books.BookStateStream(EditOnRead(data), len(data), hashlib.sha256(data).hexdigest())
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'changed during stream'):
                books.apply_book_state_locked(root, {'audio.wav': value}, [], stream_byte_limit=100)
            self.assertEqual(b'concurrent edit', target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX mode bits are not equivalent on Windows')
    def test_destination_permission_edit_during_staging_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            target.chmod(0o600)
            class ChangePermissions(io.BytesIO):
                def read(self, count=-1):
                    target.chmod(0o640)
                    return super().read(count)
            data = b'new audio'
            value = books.BookStateStream(ChangePermissions(data), len(data), hashlib.sha256(data).hexdigest())
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'changed during stream'):
                books.apply_book_state_locked(root, {'audio.wav': value}, [], stream_byte_limit=100)
            self.assertEqual(b'old audio', target.read_bytes())
            self.assertEqual(0o640, target.stat().st_mode & 0o777)

    def test_nonportable_colliding_or_recovery_targets_fail_before_staging(self):
        cases = [(['A', 'a'], []), (['å', 'a\u030a'], []), (['CON'], []),
                 (['a:b'], []), (['trailing.'], []), (['.book-switch-foreign/file'], []),
                 (['.ACTIVE_BOOK_TRANSACTION.JSON'], []), (['.ACTIVE_BOOK_TRANSACTION.JSON.LOCK'], []),
                 (['dir', 'dir/file'], []),
                 (['audio.wav'], ['.book_recovery'])]
        for names, removals in cases:
            with self.subTest(names=names, removals=removals), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                recovery = root / '.book_recovery'
                recovery.mkdir()
                (recovery / 'keep').write_bytes(b'prior book')
                replacements = {name: stream(b'new') for name in names}
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, replacements, removals, stream_byte_limit=1000)
                self.assertEqual(b'prior book', (recovery / 'keep').read_bytes())
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertFalse(list(root.glob('.book-switch-*')))

    def test_destination_changed_after_journal_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            save = books._save_journal
            def after_save(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing':
                    target.write_bytes(b'concurrent edit')
                return result
            with patch.object(books, '_save_journal', side_effect=after_save):
                with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'changed during stream'):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100)
            self.assertEqual(b'concurrent edit', target.read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(1, len(list(root.glob('.book-switch-*/new-0'))))

    def test_cancellation_after_journal_before_first_move_preserves_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            save, cancelled = books._save_journal, [False]
            def after_save(directory, document, **kwargs):
                result = save(directory, document, **kwargs)
                if document['phase'] == 'publishing':
                    cancelled[0] = True
                return result
            with patch.object(books, '_save_journal', side_effect=after_save):
                with books.ensure_book_state(root), self.assertRaises(AudioGenerationCancelled):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [],
                                                  stream_byte_limit=100, cancel_check=lambda: cancelled[0])
            self.assertEqual(b'old audio', target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_cancellation_between_targets_rolls_back_completed_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            (root / 'state.json').write_bytes(b'old state')
            move, cancelled, count = books._move, [False], [0]
            def move_and_cancel(source, destination, **kwargs):
                move(source, destination, **kwargs)
                count[0] += 1
                if count[0] == 2:
                    cancelled[0] = True
            with patch.object(books, '_move', side_effect=move_and_cancel):
                with books.ensure_book_state(root), self.assertRaises(AudioGenerationCancelled):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new'),
                        'state.json': b'new state'}, [], stream_byte_limit=100,
                        cancel_check=lambda: cancelled[0])
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertEqual(b'old state', (root / 'state.json').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_later_destination_edit_preserves_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            state = root / 'state.json'
            state.write_bytes(b'old state')
            move, count = books._move, [0]
            def move_then_edit(source, destination, **kwargs):
                move(source, destination, **kwargs)
                count[0] += 1
                if count[0] == 2:
                    state.write_bytes(b'concurrent edit')
            with patch.object(books, '_move', side_effect=move_then_edit):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new'),
                        'state.json': b'new state'}, [], stream_byte_limit=100)
            self.assertEqual(b'concurrent edit', state.read_bytes())
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual(b'old audio', (root / journal['workspace'] / 'old-0').read_bytes())
            self.assertEqual(b'new', (root / 'audio.wav').read_bytes())

    def test_edit_at_move_boundary_is_preserved_in_recovery_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            move = books._move
            changed = [False]
            def edit_before_move(source, destination, **kwargs):
                if Path(source) == target and not changed[0]:
                    target.write_bytes(b'concurrent edit')
                    changed[0] = True
                move(source, destination, **kwargs)
            with patch.object(books, '_move', side_effect=edit_before_move):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100)
            self.assertEqual(b'concurrent edit', target.read_bytes())
            journal = json.loads((root / books.JOURNAL).read_text())
            backup = root / journal['workspace'] / 'old-0'
            self.assertFalse(backup.exists())

    @unittest.skipIf(os.name == 'nt', 'Anchored book moves are not yet supported on Windows')
    def test_symlink_swapped_at_rename_does_not_write_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root, other = Path(tmp), Path(outside)
            folder = root / 'sub'
            folder.mkdir()
            target = folder / 'new.wav'
            move, swapped = books._move, [False]
            def swap_parent(source, destination, **kwargs):
                if Path(destination) == target and not swapped[0]:
                    folder.rename(root / 'original-sub')
                    folder.symlink_to(other, target_is_directory=True)
                    swapped[0] = True
                move(source, destination, **kwargs)
            with patch.object(books, '_move', side_effect=swap_parent):
                with books.ensure_book_state(root), self.assertRaises((ValueError, OSError)):
                    books.apply_book_state_locked(root, {'sub/new.wav': stream(b'private')}, [],
                                                  stream_byte_limit=100)
            self.assertTrue(swapped[0])
            self.assertFalse((other / 'new.wav').exists())
            self.assertTrue((root / 'original-sub').is_dir())

    @unittest.skipIf(os.name == 'nt', 'Anchored book moves are not yet supported on Windows')
    def test_parent_relocated_after_open_is_reported_and_evidence_preserved(self):
        import source_upload_publication as upload
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root, other = Path(tmp), Path(outside)
            folder = root / 'sub'
            folder.mkdir()
            original_move, swapped = upload.move_source_upload_exclusively, [False]
            def relocate_after_open(source, destination, **kwargs):
                if destination == 'new.wav' and not swapped[0]:
                    folder.rename(other / 'relocated')
                    folder.symlink_to(other / 'relocated', target_is_directory=True)
                    swapped[0] = True
                original_move(source, destination, **kwargs)
            with patch.object(upload, 'move_source_upload_exclusively', side_effect=relocate_after_open):
                with books.ensure_book_state(root), self.assertRaises((ValueError, OSError)):
                    books.apply_book_state_locked(root, {'sub/new.wav': stream(b'private')}, [],
                                                  stream_byte_limit=100)
            self.assertTrue(swapped[0])
            # Fail-closed: the destination parent was relocated before the
            # move, so no reverse move may return new.wav to new-0. It stays
            # in the fd-pinned relocated directory and the journal fences.
            self.assertEqual(b'private', (other / 'relocated/new.wav').read_bytes())
            self.assertFalse(list(root.glob('.book-switch-*/new-0')))
            def check():
                self.assertEqual(b'private', (other / 'relocated/new.wav').read_bytes())
            from tests.test_support import PARENT_CHANGED_MESSAGE, assert_book_journal_fenced
            assert_book_journal_fenced(self, root, content_check=check,
                expected_message=PARENT_CHANGED_MESSAGE, expected_cause_type=OSError)

    def test_file_created_at_publication_is_not_overwritten_even_with_same_bytes(self):
        for external in (b'other bytes', b'new'):
            with self.subTest(external=external), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'audio.wav'
                move = books._move
                def create_before_move(source, destination, **kwargs):
                    if Path(destination) == target:
                        target.write_bytes(external)
                    move(source, destination, **kwargs)
                with patch.object(books, '_move', side_effect=create_before_move):
                    with books.ensure_book_state(root), self.assertRaises(ValueError):
                        books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [],
                                                      stream_byte_limit=100)
                self.assertEqual(external, target.read_bytes())
                self.assertTrue((root / books.JOURNAL).exists())

    def test_published_output_edited_before_commit_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            move = books._move
            def edit_after_publish(source, destination, **kwargs):
                move(source, destination, **kwargs)
                if Path(source).name == 'new-0':
                    target.write_bytes(b'concurrent edit')
            with patch.object(books, '_move', side_effect=edit_after_publish):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100)
            self.assertEqual(b'concurrent edit', target.read_bytes())
            journal = json.loads((root / books.JOURNAL).read_text())
            self.assertEqual('publishing', journal['phase'])
            self.assertEqual(b'old audio', (root / journal['workspace'] / 'old-0').read_bytes())

    def test_committed_recovery_preserves_backup_if_output_changed_or_missing(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); save=b._save_journal
def stop(directory,document,**kwargs):
 result=save(directory,document,**kwargs)
 if document['phase']=='committed':os._exit(90)
 return result
b._save_journal=stop
data=b'new audio'; value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=100)
'''
        for change in ('modified', 'missing'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / 'audio.wav'
                target.write_bytes(b'old audio')
                child = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
                self.assertEqual(90, child.returncode, child.stderr.decode(errors='replace'))
                journal = json.loads((root / books.JOURNAL).read_text())
                if change == 'modified':
                    target.write_bytes(b'user edit')
                else:
                    target.unlink()
                with self.assertRaises(ValueError):
                    with books.ensure_book_state(root):
                        self.fail('Changed committed output was accepted')
                self.assertTrue((root / books.JOURNAL).exists())
                self.assertEqual(b'old audio', (root / journal['workspace'] / 'old-0').read_bytes())
                if change == 'modified':
                    self.assertEqual(b'user edit', target.read_bytes())

    def test_process_death_after_publishing_recovers_previous_book(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); old=b._move; count=0
def kill(source,destination,**kwargs):
 global count
 old(source,destination,**kwargs); count+=1
 if count==2: os._exit(87)
b._move=kill
data=b'new audio'; value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value,'state.json':b'new state'},[],stream_byte_limit=100)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            (root / 'state.json').write_bytes(b'old state')
            result = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(87, result.returncode, result.stderr.decode(errors='replace'))
            journal_path = root / books.JOURNAL
            original_journal = journal_path.read_bytes()
            journal = json.loads(original_journal)
            self.assertEqual(100, journal['stream_limits']['bytes'])
            journal['stream_limits']['bytes'] = True
            journal_path.write_text(json.dumps(journal))
            with self.assertRaisesRegex(ValueError, 'scan limits'):
                with books.ensure_book_state(root):
                    self.fail('Invalid recovery limit was admitted')
            journal_path.write_bytes(original_journal)
            interrupted_recovery = '''
import os,sys
from pathlib import Path
import book_state_transaction as b
original=b._move
def stop(source,destination,**kwargs):
 original(source,destination,**kwargs)
 if Path(destination).name=='discard-0': os._exit(89)
b._move=stop
with b.ensure_book_state(sys.argv[1]):pass
'''
            interrupted = subprocess.run([sys.executable, '-c', interrupted_recovery, tmp],
                                         capture_output=True, timeout=20)
            self.assertEqual(89, interrupted.returncode, interrupted.stderr.decode(errors='replace'))
            self.assertEqual(b'new audio', (root / journal['workspace'] / 'discard-0').read_bytes())
            self.assertFalse((root / 'audio.wav').exists())
            moved, cancel = books._move, [False]
            def restore_then_cancel(source, destination, **kwargs):
                moved(source, destination, **kwargs)
                if Path(source).name == 'old-0':
                    cancel[0] = True
            with patch.object(books, '_move', side_effect=restore_then_cancel):
                with self.assertRaises(AudioGenerationCancelled):
                    with books.ensure_book_state(root, cancel_check=lambda: cancel[0]):
                        self.fail('Cancelled recovery was admitted')
            self.assertTrue(journal_path.exists())
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            with patch.object(books, '_get_digest', side_effect=AssertionError('unbounded recovery read')):
                with books.ensure_book_state(root):
                    pass
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertEqual(b'old state', (root / 'state.json').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_stream_journal_downgrade_and_original_substitution_preserve_evidence(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); old=b._move; count=[0]
def stop(source,destination,**kwargs):
 old(source,destination,**kwargs);count[0]+=1
 if count[0]==2:os._exit(87)
b._move=stop
data=b'new audio'; value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=100)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            child = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(87, child.returncode, child.stderr.decode(errors='replace'))
            path = root / books.JOURNAL
            original = path.read_bytes()
            journal = json.loads(original)
            self.assertEqual(2, journal['version'])
            for change in ('missing_limits', 'downgraded_version'):
                tampered = json.loads(original)
                if change == 'missing_limits':
                    tampered.pop('stream_limits')
                else:
                    tampered['version'] = 1
                path.write_text(json.dumps(tampered))
                with self.subTest(change=change), \
                        patch.object(books, '_get_digest', side_effect=AssertionError('legacy fallback')):
                    with self.assertRaises(ValueError):
                        with books.ensure_book_state(root):
                            self.fail('Tampered journal was admitted')
                self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())
            path.write_bytes(original)
            backup = root / journal['workspace'] / 'old-0'
            replacement = root / journal['workspace'] / 'foreign'
            replacement.write_bytes(b'old audio')
            os.replace(replacement, backup)
            with self.assertRaisesRegex(ValueError, 'original artifact.*changed'):
                with books.ensure_book_state(root):
                    self.fail('Substituted original was admitted')
            self.assertTrue(path.exists())
            self.assertEqual(b'old audio', backup.read_bytes())
            self.assertEqual(b'new audio', (root / 'audio.wav').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX mode bits are not equivalent on Windows')
    def test_original_mode_change_before_recovery_is_preserved(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); old=b._move; count=[0]
def stop(source,destination,**kwargs):
 old(source,destination,**kwargs);count[0]+=1
 if count[0]==2:os._exit(87)
b._move=stop
data=b'new';value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=100)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio'); target.chmod(0o600)
            child = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(87, child.returncode, child.stderr.decode(errors='replace'))
            journal = json.loads((root / books.JOURNAL).read_text())
            backup = root / journal['workspace'] / 'old-0'
            backup.chmod(0o640)
            with self.assertRaisesRegex(ValueError, 'original artifact.*changed'):
                with books.ensure_book_state(root):
                    self.fail('Changed original mode was admitted')
            self.assertEqual(0o640, backup.stat().st_mode & 0o777)
            self.assertEqual(b'old audio', backup.read_bytes())

    def test_edit_at_rollback_move_is_preserved_for_inspection(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
root=Path(sys.argv[1]); old=b._move; count=[0]
def stop(source,destination,**kwargs):
 old(source,destination,**kwargs);count[0]+=1
 if count[0]==2:os._exit(87)
b._move=stop
data=b'new audio'; value=b.BookStateStream(io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=100)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'audio.wav'
            target.write_bytes(b'old audio')
            result = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(87, result.returncode, result.stderr.decode(errors='replace'))
            journal = json.loads((root / books.JOURNAL).read_text())
            move = books._move
            def edit_before_discard(source, destination, **kwargs):
                if Path(source) == target:
                    target.write_bytes(b'concurrent edit')
                move(source, destination, **kwargs)
            with patch.object(books, '_move', side_effect=edit_before_discard):
                with self.assertRaisesRegex(ValueError, 'replacement changed'):
                    with books.ensure_book_state(root):
                        self.fail('Changed replacement was admitted')
            workspace = root / journal['workspace']
            self.assertEqual(b'concurrent edit', (workspace / 'discard-0').read_bytes())
            self.assertEqual(b'old audio', (workspace / 'old-0').read_bytes())
            self.assertTrue((root / books.JOURNAL).exists())
            with self.assertRaisesRegex(ValueError, 'replacement changed'):
                with books.ensure_book_state(root):
                    self.fail('Changed discard was not preserved')

    def test_process_death_during_staging_leaves_previous_book_and_orphan(self):
        worker = '''
import hashlib,io,os,sys
from pathlib import Path
import book_state_transaction as b
class Die(io.BytesIO):
 calls=0
 def read(self,n=-1):
  self.calls+=1
  if self.calls==2: os._exit(88)
  return super().read(n)
root=Path(sys.argv[1]); data=b'x'*(2*1024*1024)
value=b.BookStateStream(Die(data),len(data),hashlib.sha256(data).hexdigest())
with b.ensure_book_state(root):
 b.apply_book_state_locked(root,{'audio.wav':value},[],stream_byte_limit=len(data))
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audio.wav').write_bytes(b'old audio')
            result = subprocess.run([sys.executable, '-c', worker, tmp], capture_output=True, timeout=20)
            self.assertEqual(88, result.returncode, result.stderr.decode(errors='replace'))
            with books.ensure_book_state(root):
                pass
            self.assertEqual(b'old audio', (root / 'audio.wav').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(1, len(list(root.glob('.book-switch-*'))))

    def test_stream_requires_book_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'admission'):
                books.apply_book_state_locked(tmp, {'audio.wav': stream(b'new')}, [], stream_byte_limit=100)
            self.assertFalse((Path(tmp) / 'audio.wav').exists())

    def test_reusing_one_reader_is_refused_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = stream(b'new')
            with books.ensure_book_state(root), self.assertRaisesRegex(ValueError, 'independent reader'):
                books.apply_book_state_locked(root, {'one.wav': source, 'two.wav': source}, [],
                                              stream_byte_limit=100)
            self.assertEqual(0, source.stream.tell())
            self.assertFalse(list(root.glob('.book-switch-*')))
