"""Native Windows transaction, archive and cancellation checks.

Run explicitly by the windows-owner CI job; unit discovery skips this module so
Linux shards never report these as skipped. See NATIVE_CHECKS.md.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import book_recovery_archive as archive
import book_state_transaction as books
import core
from routers import script
import windows_export_staging_directory as windows
from tests.test_script_publication_cancellation import PublicationCancellationTests


class NativeWindowsRecoveryQuotaTests(unittest.TestCase):
    def test_listing_retains_junction_and_hardlink_without_following_targets(self):
        for kind in ('junction', 'hardlink'):
            for candidate in (False, True):
                with self.subTest(kind=kind, candidate=candidate), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    storage = root / archive.ARCHIVE_DIR
                    storage.mkdir()
                    outside = root / 'foreign'
                    outside.mkdir()
                    sentinel = outside / 'sentinel'
                    sentinel.write_bytes(b'foreign evidence')
                    name = ('.pending-' + 'a' * 32 + '-' + 'b' * 32
                            if candidate else 'unknown')
                    unsafe = storage / name
                    if kind == 'junction':
                        subprocess.run(['cmd', '/c', 'mklink', '/J', str(unsafe), str(outside)],
                                       check=True, capture_output=True)
                    else:
                        os.link(sentinel, unsafe)
                    before = unsafe.lstat()
                    try:
                        listing = archive.list_recovery_records(root, quota_bytes=100000)
                        self.assertEqual(1, listing['unsafe_entries'])
                        self.assertFalse(listing['storage_complete'])
                        self.assertIsNone(listing['over_quota'])
                        self.assertEqual(0 if candidate else 1, listing['unrecognized_entries'])
                        if candidate:
                            self.assertEqual('needs_inspection', listing['records'][0]['status'])
                        else:
                            self.assertEqual([], listing['records'])
                        self.assertEqual(b'foreign evidence', sentinel.read_bytes())
                        self.assertEqual((before.st_dev, before.st_ino),
                                         (unsafe.lstat().st_dev, unsafe.lstat().st_ino))
                        refusal = ('does not accept file or directory aliases' if kind == 'junction'
                                   else 'unsafe artifact')
                        with self.assertRaisesRegex(ValueError, refusal):
                            archive._used_bytes(storage)
                        self.assertEqual(b'foreign evidence', sentinel.read_bytes())
                        self.assertEqual((before.st_dev, before.st_ino),
                                         (unsafe.lstat().st_dev, unsafe.lstat().st_ino))
                    finally:
                        if kind == 'junction':
                            os.rmdir(unsafe)

    def test_listing_marks_nested_junction_candidate_as_needing_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = root / archive.ARCHIVE_DIR
            identity = 'a' * 32
            stage_name = '.pending-' + identity + '-' + 'b' * 32
            payload = storage / stage_name / 'payload'
            payload.mkdir(parents=True)
            record = archive._marker_bytes(identity, stage_name, 'c' * 64)
            (storage / identity).write_bytes(record)
            (storage / ('prepared-' + identity)).write_bytes(record)
            before = archive.list_recovery_records(root, quota_bytes=10000)
            self.assertEqual('committed_unverified', before['records'][0]['status'])
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'sentinel').write_bytes(b'foreign evidence')
            junction = payload / 'unknown'
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                           check=True, capture_output=True)
            try:
                listing = archive.list_recovery_records(root, quota_bytes=10000)
                self.assertEqual('needs_inspection', listing['records'][0]['status'])
                self.assertEqual(1, listing['unsafe_entries'])
                self.assertFalse(listing['storage_complete'])
                self.assertEqual(b'foreign evidence', (outside / 'sentinel').read_bytes())
            finally:
                os.rmdir(junction)

    def test_listing_blocks_nested_parent_replacement_during_proof_recheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            nested = root / archive.ARCHIVE_DIR / 'nested'
            nested.mkdir(parents=True)
            target = nested / 'file'
            target.write_bytes(b'local')
            parked = nested.parent / 'parked'
            original = archive._ensure_plain_path
            checks, blocked = [], []

            def replace_during_recheck(path, admitted):
                result = original(path, admitted)
                if Path(path) == target:
                    checks.append(True)
                    if len(checks) == 2:
                        try:
                            os.rename(nested, parked)
                        except PermissionError:
                            blocked.append(True)
                        else:
                            raise AssertionError('Listing allowed nested parent replacement')
                return result

            with patch.object(archive, '_ensure_plain_path', side_effect=replace_during_recheck):
                result = archive.list_recovery_records(root, quota_bytes=1024)

            self.assertEqual([True], blocked)
            self.assertEqual(5, result['storage_bytes'])
            self.assertTrue(nested.is_dir())

    def test_windows_verifier_accepts_valid_archive_with_nested_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            storage = root / archive.ARCHIVE_DIR
            identity = 'a' * 32
            stage_name = '.pending-' + identity + '-' + 'b' * 32
            payload = storage / stage_name / 'payload'
            payload.mkdir(parents=True)
            content = b'payload'
            (payload / '0').write_bytes(content)
            manifest = {'version': 1, 'id': identity, 'book_token': 'c' * 64,
                        'created_ns': 1, 'files': [{
                            'path': 'state.json', 'size': len(content),
                            'sha256': hashlib.sha256(content).hexdigest(),
                            'source_version': [1, 1, len(content), 1, 1], 'mode': 0o600}],
                        'bytes': len(content)}
            raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
            (storage / stage_name / 'manifest.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            record = json.dumps({'version': 1, 'id': identity, 'stage': stage_name,
                                 'manifest_sha256': digest}, separators=(',', ':'))
            (storage / identity).write_text(record)
            (storage / ('prepared-' + identity)).write_text(record)

            result = archive.verify_recovery_archive(root, identity,
                expected_manifest_sha256=digest, quota_bytes=1024 * 1024)

            self.assertEqual(identity, result['id'])
            self.assertEqual(1, result['files'])
            self.assertEqual(len(content), result['bytes'])

    def test_verification_blocks_nested_payload_parent_replacement(self):
        import audio_take_history as audio
        import project_scope_files as scope
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            storage = root / archive.ARCHIVE_DIR
            stage_name = '.pending-' + 'a' * 32 + '-' + 'b' * 32
            payload = storage / stage_name / 'payload'
            payload.mkdir(parents=True)
            (payload / '0').write_bytes(b'local payload')
            parked = storage / 'parked-payload'
            outside = Path(temporary) / 'outside'
            outside.mkdir()
            (outside / '0').write_bytes(b'external payload')
            original = audio._ensure_plain_path
            swapped, blocked = [], []

            def replace_after_path_check(path, admitted):
                result = original(path, admitted)
                if Path(path) == payload / '0' and not swapped:
                    try:
                        os.rename(payload, parked)
                    except PermissionError:
                        blocked.append(True)
                    else:
                        subprocess.run(['cmd', '/c', 'mklink', '/J', str(payload), str(outside)],
                                       check=True, capture_output=True)
                        swapped.append(True)
                return result
            try:
                from windows_export_staging_directory import (WindowsBookRoot,
                    guard_windows_book_move_parents)
                storage_info = storage.lstat()
                root_guard = WindowsBookRoot(storage, (storage_info.st_dev, storage_info.st_ino))
                placeholder = payload / '.recovery-verify-scan'
                with root_guard, \
                     guard_windows_book_move_parents(storage, placeholder, placeholder,
                                                     root_guard=root_guard), \
                     patch.object(audio, '_ensure_plain_path', side_effect=replace_after_path_check), \
                     patch.object(scope, '_ensure_plain_path', side_effect=replace_after_path_check):
                    scope.capture_scope_file(storage, stage_name + '/payload/0')
                self.assertTrue(blocked)
                self.assertEqual([], swapped)
            finally:
                if swapped and payload.exists():
                    os.rmdir(payload)

    def test_path_quota_scan_bounds_directory_depth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'a' / 'b' / 'c').mkdir(parents=True)
            with patch.object(archive, 'MAX_QUOTA_DEPTH', 2):
                with self.assertRaisesRegex(ValueError, 'directory depth limit'):
                    archive._used_bytes(root)

    def test_verification_guards_storage_during_receipt_read(self):
        import audio_take_history as audio
        import project_scope_files as scope
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            storage, parked = root / archive.ARCHIVE_DIR, root / 'parked'
            storage.mkdir()
            identity = 'a' * 32
            (storage / ('prepared-' + identity)).write_bytes(b'local receipt')
            outside = Path(temporary) / 'outside'
            outside.mkdir()
            sentinel = outside / ('prepared-' + identity)
            sentinel.write_bytes(b'external')
            original = audio._ensure_plain_path
            checks, blocked = [], []
            def try_replace(path, admitted):
                result = original(path, admitted)
                if Path(path).name == 'prepared-' + identity:
                    checks.append(True)
                    if len(checks) == 2:
                        try:
                            os.rename(storage, parked)
                        except PermissionError:
                            blocked.append(True)
                        else:
                            raise AssertionError('Verification allowed archive replacement')
                return result
            with patch.object(audio, '_ensure_plain_path', side_effect=try_replace), \
                 patch.object(scope, '_ensure_plain_path', side_effect=try_replace):
                with self.assertRaises(ValueError):
                    archive.verify_recovery_archive(root, identity,
                        expected_manifest_sha256='b' * 64, quota_bytes=1024)
            self.assertEqual([True], blocked)
            self.assertEqual(b'external', sentinel.read_bytes())
            self.assertFalse(parked.exists())

    def test_quota_scan_rejects_junction_inserted_after_path_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage, outside = root / 'storage', root / 'outside'
            storage.mkdir()
            outside.mkdir()
            (outside / 'sentinel').write_bytes(b'external')
            child, parked = storage / 'child', storage / 'parked'
            child.mkdir()
            ensure_plain = archive._ensure_plain_path
            swapped = []

            def replace_after_path_check(path, admitted):
                result = ensure_plain(path, admitted)
                if path == child and not swapped:
                    os.rename(child, parked)
                    subprocess.run(['cmd', '/c', 'mklink', '/J', str(child), str(outside)],
                                   check=True, capture_output=True)
                    swapped.append(True)
                return result
            try:
                with patch.object(archive, '_ensure_plain_path', side_effect=replace_after_path_check):
                    with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
                        archive._used_bytes(storage)
                self.assertEqual([True], swapped)
                self.assertEqual(b'external', (outside / 'sentinel').read_bytes())
            finally:
                if swapped:
                    os.rmdir(child)

    def test_listing_guards_storage_after_quota_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage, parked = root / archive.ARCHIVE_DIR, root / 'parked'
            storage.mkdir()
            original = Path.iterdir
            calls, blocked = [], []
            def replace_before_listing(path):
                if path == storage:
                    calls.append(True)
                    if len(calls) == 2:
                        try:
                            os.rename(storage, parked)
                        except PermissionError:
                            blocked.append(True)
                        else:
                            raise AssertionError('Listing allowed archive replacement')
                return original(path)
            with patch.object(Path, 'iterdir', replace_before_listing):
                result = archive.list_recovery_records(root, quota_bytes=1024)
            self.assertEqual([True], blocked)
            self.assertEqual([], result['records'])
            self.assertTrue(storage.is_dir())
            self.assertFalse(parked.exists())

    def test_quota_scan_refuses_replaced_queued_directory(self):
        from windows_export_staging_directory import guard_windows_book_move_parents
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child, parked = root / 'child', root / 'parked'
            child.mkdir()
            (child / 'original').write_bytes(b'original')
            swapped = []

            @contextlib.contextmanager
            def replace_before_guard(guard_root, source, destination):
                if Path(source).parent == child:
                    os.rename(child, parked)
                    child.mkdir()
                    swapped.append(True)
                with guard_windows_book_move_parents(guard_root, source, destination):
                    yield

            with patch('windows_export_staging_directory.guard_windows_book_move_parents',
                       side_effect=replace_before_guard):
                with self.assertRaisesRegex(ValueError, 'changed during quota scan'):
                    archive._used_bytes(root)
            self.assertEqual([True], swapped)
            self.assertEqual(b'original', (parked / 'original').read_bytes())

    def test_quota_scan_guards_parent_during_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child, parked = root / 'child', root / 'parked'
            child.mkdir()
            (child / '.recovery-quota-scan').write_bytes(b'x')
            original = Path.iterdir
            attempts = []

            def replace_during_scan(path):
                if path == child:
                    try:
                        os.rename(child, parked)
                    except PermissionError:
                        attempts.append('blocked')
                    else:
                        raise AssertionError('Quota scan allowed a parent replacement')
                return original(path)

            with patch.object(Path, 'iterdir', replace_during_scan):
                self.assertEqual(1, archive._used_bytes(root))
            self.assertEqual(['blocked'], attempts)
            self.assertTrue(child.is_dir())
            self.assertFalse(parked.exists())


class NativeWindowsBookStateTests(unittest.TestCase):
    def test_byte_staging_failure_cleans_native_owned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'first').write_bytes(b'old first')
            (root / 'second').write_bytes(b'old second')
            publish = books.save_adapter_publication_bytes
            calls = []
            def fail_second(path, value, mode):
                calls.append(Path(path).name)
                if len(calls) == 2:
                    raise OSError('injected byte staging failure')
                return publish(path, value, mode)
            with patch.object(books, 'save_adapter_publication_bytes',
                              side_effect=fail_second):
                with books.ensure_book_state(root), \
                        self.assertRaisesRegex(OSError, 'injected byte staging failure'):
                    books.apply_book_state_locked(
                        root, {'first':b'new first', 'second':b'new second'}, [])
            self.assertEqual(b'old first', (root / 'first').read_bytes())
            self.assertEqual(b'old second', (root / 'second').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    def test_byte_staging_failure_preserves_changed_stage_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish = books.save_adapter_publication_bytes
            calls = []
            def change_first_then_fail(path, value, mode):
                calls.append(Path(path))
                if len(calls) == 2:
                    calls[0].write_bytes(b'foreign change')
                    raise OSError('fixture staging error')
                return publish(path, value, mode)
            with patch.object(books, 'save_adapter_publication_bytes', side_effect=change_first_then_fail):
                with books.ensure_book_state(root), self.assertRaises(ValueError) as raised:
                    books.apply_book_state_locked(root, {'one': b'first', 'two': b'second'}, [])
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn('version_changed=True', str(raised.exception))
            self.assertEqual(b'foreign change', calls[0].read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_windows_stage_handle_failure_preserves_workspace(self):
        import windows_export_staging_directory as native
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_handle = native._Handle
            calls = []
            def fail_final_open(api, path, **kwargs):
                if Path(path).name == 'new-0':
                    calls.append(1)
                    if len(calls) == 3:
                        raise OSError('fixture stage handle failure')
                return real_handle(api, path, **kwargs)
            source = books.BookStateStream(io.BytesIO(b'first'), 5, hashlib.sha256(b'first').hexdigest())
            with patch.object(native, '_Handle', side_effect=fail_final_open):
                with books.ensure_book_state(root), self.assertRaisesRegex(
                        ValueError, 'version unavailable') as raised:
                    books.apply_book_state_locked(root, {'one': source}, [], stream_byte_limit=1024)
            self.assertEqual(3, len(calls))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            staged = list(root.glob('.book-switch-*/new-0'))
            self.assertEqual(1, len(staged))
            self.assertEqual(b'first', staged[0].read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_windows_handle_failure_chains_short_read_and_preserves_stage(self):
        import windows_export_staging_directory as native
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_handle = native._Handle
            calls = []
            def fail_final_open(api, path, **kwargs):
                if Path(path).name == 'new-0':
                    calls.append(1)
                    if len(calls) == 3:
                        raise OSError('fixture stage handle failure')
                return real_handle(api, path, **kwargs)
            source = books.BookStateStream(io.BytesIO(b'a'), 3, hashlib.sha256(b'abc').hexdigest())
            with patch.object(native, '_Handle', side_effect=fail_final_open):
                with books.ensure_book_state(root), self.assertRaisesRegex(
                        ValueError, 'version unavailable') as raised:
                    books.apply_book_state_locked(root, {'one': source}, [], stream_byte_limit=1024)
            handle_error = raised.exception.__cause__
            self.assertIsInstance(handle_error, OSError)
            self.assertIsInstance(handle_error.__context__, ValueError)
            self.assertIn('ended early', str(handle_error.__context__))
            self.assertEqual(b'a', next(root.glob('.book-switch-*/new-0')).read_bytes())

    def test_windows_replaced_stage_after_writer_close_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_close = os.close
            swapped = []
            def replace_after_close(descriptor):
                try:
                    info = os.fstat(descriptor)
                    matches = [path for path in root.glob('.book-switch-*/new-0')
                               if (path.stat().st_dev, path.stat().st_ino) == (info.st_dev, info.st_ino)]
                except OSError:
                    matches = []
                real_close(descriptor)
                if matches and not swapped:
                    path = matches[0]
                    path.rename(path.with_name('retained'))
                    path.write_bytes(b'foreign change')
                    swapped.append(True)
            source = books.BookStateStream(io.BytesIO(b'first'), 5, hashlib.sha256(b'first').hexdigest())
            with patch.object(books.os, 'close', side_effect=replace_after_close):
                with books.ensure_book_state(root), self.assertRaises(ValueError):
                    books.apply_book_state_locked(root, {'one': source}, [], stream_byte_limit=1024)
            self.assertTrue(swapped)
            workspace = next(root.glob('.book-switch-*'))
            self.assertEqual(b'first', (workspace / 'retained').read_bytes())
            self.assertEqual(b'foreign change', (workspace / 'new-0').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())


class NativeWindowsBookMoveTests(unittest.TestCase):
    def test_native_file_identity_survives_creation_handle_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'staged'
            with path.open('xb') as output:
                created = windows.windows_named_file_identity(path)
                output.write(b'owned')
                output.flush()
                os.fsync(output.fileno())
                written = windows.windows_named_file_identity(path)
                opened_stat = os.fstat(output.fileno())
            closed = windows.windows_named_file_identity(path)
            named_stat = path.lstat()
            self.assertEqual(
                (created, created),
                (written, closed),
                {'created':created, 'written':written, 'closed':closed,
                 'opened_stat':(opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_nlink),
                 'named_stat':(named_stat.st_dev, named_stat.st_ino, named_stat.st_nlink)})

    def test_recovery_guard_blocks_metadata_unlink_and_directory_removal(self):
        from export_staging_directory import OWNER_FILE
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)/'stage'; stage.mkdir()
            (stage/OWNER_FILE).write_bytes(b'owner')
            (stage/'.export-staging.json').write_bytes(b'{}')
            info = stage.lstat()
            with windows.WindowsStageDirectory(stage, (info.st_dev, info.st_ino)) as guarded:
                guarded.create_recovery_guard('.export-staging-recovery')
                for name in ('.export-staging-recovery', OWNER_FILE, '.export-staging.json'):
                    with self.subTest(name=name), self.assertRaises(windows.StageDirectoryChanged):
                        guarded.unlink(name)
                    self.assertTrue((stage/name).exists())
                with self.assertRaises(windows.StageDirectoryChanged):
                    guarded.remove_empty_directory()

    def test_native_byte_and_stream_transactions_finish_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'artifact.bin'
            target.write_bytes(b'old')
            with books.ensure_book_state(root):
                books.apply_book_state_locked(root, {'artifact.bin': b'byte replacement'}, [])
            self.assertEqual(b'byte replacement', target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

            raw = b'stream replacement'
            source = books.BookStateStream(io.BytesIO(raw), len(raw),
                                           hashlib.sha256(raw).hexdigest())
            with books.ensure_book_state(root):
                books.apply_book_state_locked(root, {'artifact.bin': source}, [],
                                              stream_byte_limit=1024)
            self.assertEqual(raw, target.read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertFalse(list(root.glob('.book-switch-*')))

    def test_native_handles_deny_parent_rename_until_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            parent = root / 'parent'
            parent.mkdir()
            with windows.guard_windows_book_move_parents(root, parent / 'source', parent / 'destination'):
                with self.assertRaises(OSError):
                    parent.rename(root / 'moved')
            parent.rename(root / 'moved')
            self.assertTrue((root / 'moved').is_dir())


class NativeWindowsPublicationCancellationTests(unittest.TestCase):
    fixture = PublicationCancellationTests.fixture

    def test_published_cleanup_accepts_job_already_closed_after_confirmed_stop(self):
        from windows_subprocess_job import WindowsJobClosedError
        with self.fixture() as (root, states, old, publish):
            publish()
            process = SimpleNamespace(pid=123, _alexandria_windows_owner=object())
            states['script'].update(process=process, pid=process.pid)
            with patch.object(script, '_cancel_task', side_effect=HTTPException(400, 'Script generation process already exited.')), \
                 patch.object(core, 'terminate_windows_process_tree', side_effect=WindowsJobClosedError('closed')):
                self.assertEqual({'status': 'published_cleanup_stop', 'worker': 'already stopped'},
                                 script._cancel_script_generation_sync())
            self.assertNotEqual(old, (root / 'annotated_script.json').read_bytes())

    def test_published_cleanup_force_stop_failure_remains_unconfirmed(self):
        for force_fails in (False, True):
            with self.subTest(force_fails=force_fails), self.fixture() as (root, states, old, publish):
                publish()
                process = SimpleNamespace(pid=123, _alexandria_windows_owner=object())
                states['script'].update(process=process, pid=process.pid)
                with patch.object(script, '_cancel_task', side_effect=HTTPException(400, 'Script generation process already exited.')), \
                     patch.object(core, 'terminate_windows_process_tree',
                                  side_effect=OSError('force failed') if force_fails else None) as force:
                    if force_fails:
                        with self.assertRaises(HTTPException) as raised:
                            script._cancel_script_generation_sync()
                        self.assertEqual(409, raised.exception.status_code)
                    else:
                        self.assertEqual({'status': 'published_cleanup_stop_requested', 'worker': 'force stop requested'},
                                         script._cancel_script_generation_sync())
                    force.assert_called_once_with(process, force=True)
                self.assertNotEqual(old, (root / 'annotated_script.json').read_bytes())

    def test_unrelated_cancel_refusal_does_not_force_stop(self):
        with self.fixture() as (root, states, old, publish):
            publish()
            process = SimpleNamespace(pid=123, _alexandria_windows_owner=object())
            states['script'].update(process=process, pid=process.pid)
            for refusal in (HTTPException(409, 'ownership changed'), HTTPException(400, 'different reason')):
                with self.subTest(detail=refusal.detail), \
                     patch.object(script, '_cancel_task', side_effect=refusal), \
                     patch.object(core, 'terminate_windows_process_tree') as force:
                    with self.assertRaises(HTTPException) as raised:
                        script._cancel_script_generation_sync()
                    self.assertEqual(409, raised.exception.status_code)
                    force.assert_not_called()
            self.assertNotEqual(old, (root / 'annotated_script.json').read_bytes())


class NativeWindowsPathAdmissionTests(unittest.TestCase):
    def test_unreadable_ancestor_is_refused_by_shared_admission(self):
        from direct_path_admission import is_plain_path
        from project_scope_files import capture_scope_file
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = Path.lstat
            def denied(path, *args, **kwargs):
                if path == root.parent:
                    raise PermissionError('synthetic ancestor denial')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'lstat', denied):
                self.assertFalse(is_plain_path(root))
                with self.assertRaisesRegex(ValueError, 'aliases'):
                    capture_scope_file(root, 'state.json')

    def test_short_names_agree_across_book_scope_take_and_recovery_admission(self):
        import ctypes
        from project_scope_files import capture_scope_file
        from audio_take_history import _open_regular
        from book_recovery_archive import _direct_root
        from direct_path_admission import is_plain_path
        with tempfile.TemporaryDirectory(prefix='aa2-long-short-name-regression-') as tmp:
            root = Path(tmp)
            (root / 'state.json').write_bytes(b'{}')
            api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            api.restype = ctypes.c_uint32
            needed = api(str(root), None, 0)
            if not needed:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(needed)
            if not api(str(root), buffer, needed):
                raise ctypes.WinError(ctypes.get_last_error())
            short = Path(buffer.value)
            if os.path.normcase(str(short)) == os.path.normcase(str(root)):
                self.fail('Native qualification requires a volume with distinct DOS short names')
            self.assertTrue(os.path.samefile(root, short))
            self.assertTrue(is_plain_path(short))
            self.assertEqual(short, books._direct_book_root(short)[0])
            self.assertEqual(short, _direct_root(short))
            self.assertEqual(b'{}', capture_scope_file(short, 'state.json', include_bytes=True)[1])
            stream, _ = _open_regular(short / 'state.json', short, 1024)
            with stream:
                self.assertEqual(b'{}', stream.read())


NATIVE_TEST_COUNT = 25


def main():
    if sys.platform != 'win32':
        print('Native Windows transaction checks require Windows; nothing was verified.', file=sys.stderr)
        return 2
    # Hosted runners set TEMP through a DOS 8.3 short name (RUNNER~1).
    # General fixtures use the long form. The dedicated path-admission test
    # obtains a distinct short spelling and checks it explicitly.
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (
            NativeWindowsRecoveryQuotaTests, NativeWindowsBookStateTests, NativeWindowsBookMoveTests, NativeWindowsPublicationCancellationTests,
            NativeWindowsPathAdmissionTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if (result.wasSuccessful() and result.testsRun == NATIVE_TEST_COUNT
                 and not result.skipped) else 1


if __name__ == '__main__':
    raise SystemExit(main())
