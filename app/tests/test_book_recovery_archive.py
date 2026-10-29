"""Bounded immutable archive primitives; these do not qualify book restore."""
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core
import book_recovery_archive as archive
from book_state_transaction import get_book_operation_token
from project_scope_admission import ensure_scope_admission


class BookRecoveryArchiveTests(unittest.TestCase):
    def test_portable_artifact_names_reject_windows_invalid_and_control_characters(self):
        import book_recovery_archive as archive
        for name in ('bad?name', 'bad*name', 'bad<name', 'bad>name', 'bad|name',
                     'bad"name', 'bad\nname', 'bad\x1fname'):
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                archive._relative_name(name)
    def setUp(self):
        try:
            archive._require_secure_staging()
        except ValueError as error:
            self.skipTest(str(error))

    @staticmethod
    def rebind_test_marker(root, receipt):
        storage=root/archive.ARCHIVE_DIR; manifest=storage/receipt['stage']/'manifest.json'
        digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
        for marker in (storage/receipt['id'],storage/('prepared-'+receipt['id'])):
            value=json.loads(marker.read_text()); value['manifest_sha256']=digest
            marker.write_text(json.dumps(value))
        return digest

    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'state.json').write_text('{"active_book_id":"A","book_generation":"one"}')
            (root/'annotated_script.json').write_text('[{"text":"Prior book."}]')
            (root/'chunks.json').write_text('[]')
            (root/'audio').mkdir(); (root/'audio/line.wav').write_bytes(b'large-audio' * 100_000)
            with patch.object(core, 'DATA_DIR', str(root)), \
                    patch.object(core, 'ensure_book_switch_allowed', return_value=contextlib.nullcontext()):
                with ensure_scope_admission(root):
                    yield root, get_book_operation_token(root)

    @unittest.skipIf(os.name == 'nt', 'POSIX resource limits required')
    def test_pinned_quota_scan_deep_tree_uses_bounded_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            current = root
            for _ in range(80):
                current = current / 'd'
                current.mkdir()
            (current / 'file').write_bytes(b'x')
            code = r"""import os,resource,sys
from pathlib import Path
import book_recovery_archive as archive
root=Path(sys.argv[1]); fd=archive._open_directory(str(root))
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
limit=64 if hard == resource.RLIM_INFINITY else min(64, hard)
resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
try:
 assert archive._used_bytes(root,directory_fd=fd)==1
 assert archive._used_bytes(root,directory_fd=fd)==1
finally: os.close(fd)
"""
            result = subprocess.run([sys.executable, '-c', code, str(root)],
                                    cwd=Path(__file__).parent.parent, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_pinned_quota_scan_rejects_links_and_bounds_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / 'storage'
            storage.mkdir()
            (storage / 'first').write_bytes(b'a')
            (storage / 'second').write_bytes(b'b')
            descriptor = archive._open_directory(str(storage))
            try:
                with self.assertRaisesRegex(ValueError, 'too many entries'):
                    archive._used_bytes(storage, directory_fd=descriptor, max_entries=1)
                (storage / 'second').unlink()
                outside = root / 'outside'
                outside.write_bytes(b'private')
                (storage / 'link').symlink_to(outside)
                with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
                    archive._used_bytes(storage, directory_fd=descriptor)
                self.assertEqual(b'private', outside.read_bytes())
            finally:
                os.close(descriptor)

    def test_pinned_quota_scan_refuses_change_before_reopened_directory_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            child = root / 'child'
            child.mkdir()
            identity = (child.stat().st_dev, child.stat().st_ino)
            descriptor = archive._open_directory(str(root))
            original_fstat = os.fstat
            observations = []
            def change_after_reopen(fd):
                info = original_fstat(fd)
                if (info.st_dev, info.st_ino) == identity:
                    observations.append(True)
                    if len(observations) == 2:
                        (child / 'new').write_bytes(b'foreign')
                        # Force the injected directory-version change even on
                        # a VM/filesystem whose clock has not advanced yet.
                        os.utime(child, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                return info
            try:
                with patch.object(archive.os, 'fstat', side_effect=change_after_reopen):
                    with self.assertRaisesRegex(ValueError, 'changed during quota scan'):
                        archive._used_bytes(root, directory_fd=descriptor)
                self.assertGreaterEqual(len(observations), 2)
                self.assertEqual(b'foreign', (child / 'new').read_bytes())
            finally:
                os.close(descriptor)

    def test_pinned_quota_scan_ignores_substituted_archive_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / 'storage'
            storage.mkdir()
            (storage / 'nested').mkdir()
            (storage / 'nested' / 'owned').write_bytes(b'1234567')
            descriptor = archive._open_directory(str(storage))
            try:
                storage.rename(root / 'displaced')
                storage.mkdir()
                (storage / 'foreign').write_bytes(b'1')
                self.assertEqual(7, archive._used_bytes(storage, directory_fd=descriptor))
                self.assertEqual(7, archive._used_bytes(storage, directory_fd=descriptor))
                self.assertEqual(1, archive._used_bytes(storage))
            finally:
                os.close(descriptor)

    def test_archive_staging_refuses_swapped_storage_directory(self):
        with self.fixture() as (root, token):
            storage = root / archive.ARCHIVE_DIR
            displaced = root / 'displaced-storage'
            used_bytes = archive._used_bytes
            swapped = []
            def swap_after_usage(directory, **kwargs):
                result = used_bytes(directory, **kwargs)
                if not swapped:
                    storage.rename(displaced)
                    storage.mkdir(mode=0o700)
                    (storage / 'foreign').write_bytes(b'preserve')
                    swapped.append(True)
                return result
            with patch.object(archive, '_used_bytes', side_effect=swap_after_usage):
                with self.assertRaisesRegex(ValueError, 'storage changed'):
                    archive.create_recovery_archive(root, ['state.json'], book_token=token,
                                                    quota_bytes=10 * 1024**2)
            self.assertTrue(swapped)
            self.assertEqual(['foreign'], sorted(p.name for p in storage.iterdir()))
            self.assertEqual(b'preserve', (storage / 'foreign').read_bytes())
            self.assertFalse(list(displaced.glob('.pending-*')))

    def test_archive_accepts_empty_artifact_with_zero_reserved_bytes(self):
        with self.fixture() as (root, token):
            (root / 'empty.txt').write_bytes(b'')
            receipt = archive.create_recovery_archive(root, ['empty.txt'], book_token=token,
                                                      quota_bytes=10 * 1024**2)
            verified = archive.verify_recovery_archive(root, receipt['id'],
                expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=10 * 1024**2)
            self.assertEqual(1, verified['files'])
            self.assertEqual(b'', (root / archive.ARCHIVE_DIR / receipt['stage'] / 'payload' / '0').read_bytes())

    def test_growth_after_reservation_cannot_consume_remaining_quota(self):
        with self.fixture() as (root, token):
            target = root / 'chunks.json'
            original_size = target.stat().st_size
            copy = archive._copy_artifact
            limits = []
            def grow_before_copy(directory, name, destination, remaining, cancel_check, **kwargs):
                limits.append(remaining)
                target.write_bytes(b'x' * 8192)
                return copy(directory, name, destination, remaining, cancel_check, **kwargs)
            with patch.object(archive, '_copy_artifact', side_effect=grow_before_copy):
                with self.assertRaises(ValueError):
                    archive.create_recovery_archive(root, ['chunks.json'], book_token=token,
                                                    quota_bytes=10 * 1024**2)
            self.assertEqual([original_size], limits)
            pending = list((root / archive.ARCHIVE_DIR).glob('.pending-*/payload/*'))
            self.assertTrue(all(p.stat().st_size <= original_size for p in pending))
            self.assertEqual(b'x' * 8192, target.read_bytes())

    def test_streamed_archive_verifies_exact_files_and_detects_corruption(self):
        with self.fixture() as (root, token):
            names=['state.json','annotated_script.json','chunks.json','audio/line.wav']
            receipt=archive.create_recovery_archive(root,names,book_token=token,quota_bytes=10*1024**2)
            self.assertEqual(4,receipt['files'])
            checked=archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=10*1024**2)
            self.assertEqual(token,checked['book_token']);self.assertEqual(receipt['manifest_sha256'],checked['manifest_sha256'])
            with self.assertRaisesRegex(ValueError, 'differs from the expected observation'):
                archive.verify_recovery_archive(root, receipt['id'],
                    expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=10*1024**2,
                    expected_observation_revision='a' * 64)
            bundle=root/archive.ARCHIVE_DIR/receipt['stage']; manifest=json.loads((bundle/'manifest.json').read_text())
            for index,name in enumerate(names):
                raw=(bundle/'payload'/str(index)).read_bytes()
                self.assertEqual((root/name).read_bytes(),raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['files'][index]['sha256'])
            (bundle/'payload/3').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'differs'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=10*1024**2)

    def test_inventory_bound_archive_copies_exact_proven_files(self):
        from project_scope_files import capture_scope_file
        with self.fixture() as (root, token):
            names = ['state.json', 'annotated_script.json', 'audio/line.wav']
            proofs = [capture_scope_file(root, name, limit=10 * 1024**2)[0] for name in names]
            receipt = archive.create_recovery_archive(root, names, book_token=token,
                quota_bytes=10 * 1024**2, expected_proofs=proofs)
            result = archive.verify_recovery_archive(root, receipt['id'],
                expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=10 * 1024**2)
            self.assertEqual(3, result['files'])
            manifest = json.loads((root / archive.ARCHIVE_DIR / receipt['stage'] / 'manifest.json').read_text())
            self.assertEqual({proof['path']: proof['sha256'] for proof in proofs},
                             {row['path']: row['sha256'] for row in manifest['files']})

    def test_observation_manifest_cannot_claim_full_book_recovery(self):
        from project_scope_files import capture_scope_file
        with self.fixture() as (root, token):
            proof = capture_scope_file(root, 'state.json', limit=100000)[0]
            receipt = archive.create_recovery_archive(root, ['state.json'], book_token=token,
                quota_bytes=10000000, expected_proofs=[proof], observation_revision='a' * 64,
                _prepublication_check=lambda: None)
            path = root / archive.ARCHIVE_DIR / receipt['stage'] / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['observation_complete'] = True
            path.write_text(json.dumps(manifest))
            digest = self.rebind_test_marker(root, receipt)
            with self.assertRaisesRegex(ValueError, 'Invalid recovery manifest structure'):
                archive.verify_recovery_archive(root, receipt['id'],
                    expected_manifest_sha256=digest, quota_bytes=10000000)

    def test_changed_or_forged_inventory_never_publishes_archive(self):
        from project_scope_files import capture_scope_file
        for change in ('changed_file', 'wrong_digest', 'missing_proof', 'duplicate_proof'):
            with self.subTest(change=change), self.fixture() as (root, token):
                names = ['state.json', 'annotated_script.json']
                proofs = [capture_scope_file(root, name, limit=100000)[0] for name in names]
                if change == 'changed_file':
                    (root / names[1]).write_bytes(b'changed user script')
                elif change == 'wrong_digest':
                    proofs[1]['sha256'] = '0' * 64
                elif change == 'missing_proof':
                    proofs.pop()
                else:
                    proofs[1] = dict(proofs[0])
                with self.assertRaises(ValueError):
                    archive.create_recovery_archive(root, names, book_token=token,
                        quota_bytes=100000, expected_proofs=proofs)
                storage = root / archive.ARCHIVE_DIR
                if storage.exists():
                    self.assertFalse(any(archive._ID.fullmatch(path.name) for path in storage.iterdir()))
                if change == 'changed_file':
                    self.assertEqual(b'changed user script', (root / names[1]).read_bytes())

    def test_caller_cannot_change_bound_inventory_during_copy(self):
        from project_scope_files import capture_scope_file
        with self.fixture() as (root, token):
            names = ['state.json', 'annotated_script.json']
            proofs = [capture_scope_file(root, name, limit=100000)[0] for name in names]
            expected = proofs[1]['sha256']
            copy = archive._copy_artifact
            def mutate_caller(*args, **kwargs):
                result = copy(*args, **kwargs)
                proofs[1]['sha256'] = '0' * 64
                return result
            with patch.object(archive, '_copy_artifact', side_effect=mutate_caller):
                receipt = archive.create_recovery_archive(root, names, book_token=token,
                    quota_bytes=100000, expected_proofs=proofs)
            manifest = json.loads((root / archive.ARCHIVE_DIR / receipt['stage'] / 'manifest.json').read_text())
            self.assertEqual(expected, manifest['files'][1]['sha256'])

    def test_requires_task_admission_current_identity_and_bounded_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'state.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'admission'):
                archive.create_recovery_archive(root,['state.json'],book_token='a'*64,quota_bytes=10000)
        with self.fixture() as (root,token):
            with self.assertRaisesRegex(ValueError,'book changed'):
                archive.create_recovery_archive(root,['state.json'],book_token='a'*64,quota_bytes=10000)
            before=(root/'audio/line.wav').read_bytes()
            with self.assertRaises(ValueError):
                archive.create_recovery_archive(root,['audio/line.wav'],book_token=token,quota_bytes=1024)
            self.assertEqual(before,(root/'audio/line.wav').read_bytes())
            storage=root/archive.ARCHIVE_DIR
            self.assertFalse(any(p.is_dir() for p in storage.iterdir()))

    def test_revoked_admission_prevents_publication(self):
        with self.fixture() as (root, token):
            with patch.object(archive, 'is_scope_admitted', side_effect=[True, False]):
                with self.assertRaisesRegex(ValueError, 'admission ended'):
                    archive.create_recovery_archive(root, ['state.json'],
                                                   book_token=token, quota_bytes=100000)
            storage = root / archive.ARCHIVE_DIR
            self.assertFalse(any(archive._ID.fullmatch(path.name) for path in storage.iterdir()))
            self.assertTrue(any(path.name.startswith('prepared-') for path in storage.iterdir()))

    def test_book_identity_read_is_bounded_before_archive_creation(self):
        with self.fixture() as (root, token):
            with patch.object(archive, 'MAX_BOOK_STATE_BYTES', 8):
                with self.assertRaises(ValueError):
                    archive.create_recovery_archive(root, ['annotated_script.json'],
                                                   book_token=token, quota_bytes=100000)
            self.assertFalse((root / archive.ARCHIVE_DIR).exists())

    def test_missing_active_state_cannot_authorize_archive(self):
        with self.fixture() as (root, _):
            (root / 'state.json').unlink()
            token = get_book_operation_token(root)
            with self.assertRaisesRegex(ValueError, 'existing active book state'):
                archive.create_recovery_archive(root, ['annotated_script.json'],
                                               book_token=token, quota_bytes=100000)
            self.assertFalse((root / archive.ARCHIVE_DIR).exists())

    @unittest.skipIf(os.name == 'nt', 'secure POSIX staging required')
    def test_archive_directory_swap_cannot_create_lock_outside_project(self):
        with self.fixture() as (root, token), tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary).resolve()
            original = archive._archive_directory
            def substitute(*args, **kwargs):
                directory = original(*args, **kwargs)
                directory.rename(root / '.recovery-retained')
                directory.symlink_to(outside, target_is_directory=True)
                return directory
            with patch.object(archive, '_archive_directory', side_effect=substitute):
                with self.assertRaises((ValueError, OSError)):
                    archive.create_recovery_archive(root, ['state.json'], book_token=token, quota_bytes=100000)
            self.assertFalse((outside / 'archive.lock').exists())

    @unittest.skipIf(os.name == 'nt', 'secure POSIX staging required')
    def test_project_root_replacement_cannot_redirect_archive_creation(self):
        with tempfile.TemporaryDirectory() as parent:
            base = Path(parent).resolve(); root = base/'book'; root.mkdir()
            (root/'state.json').write_text(json.dumps({
                'active_book_id':'book', 'book_generation':'generation'}))
            (root/'artifact').write_bytes(b'original')
            token = archive._capture_book_token(root, None)
            retained = base/'retained'; create = archive._archive_directory; swapped=[]
            def replace_root(*args, **kwargs):
                if not swapped:
                    root.rename(retained); root.mkdir()
                    (root/'state.json').write_text(json.dumps({
                        'active_book_id':'book', 'book_generation':'generation'}))
                    (root/'artifact').write_bytes(b'foreign')
                    swapped.append(True)
                return create(*args, **kwargs)
            from project_scope_admission import ensure_scope_admission
            with patch.object(core, 'DATA_DIR', str(root)), ensure_scope_admission(root), \
                    patch.object(archive, '_archive_directory', side_effect=replace_root):
                with self.assertRaisesRegex(ValueError, 'root changed'):
                    archive.create_recovery_archive(
                        root, ['artifact'], book_token=token, quota_bytes=100000)
            self.assertFalse((root/archive.ARCHIVE_DIR).exists())
            self.assertEqual(b'foreign', (root/'artifact').read_bytes())
            self.assertEqual(b'original', (retained/'artifact').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'secure POSIX staging required')
    def test_symlinked_archive_lock_does_not_open_its_target(self):
        with self.fixture() as (root, token), tempfile.TemporaryDirectory() as temporary:
            storage = archive._archive_directory(root)
            target = Path(temporary).resolve() / 'target'
            (storage / 'archive.lock').symlink_to(target)
            with self.assertRaises((ValueError, OSError)):
                archive.create_recovery_archive(root, ['state.json'], book_token=token, quota_bytes=100000)
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX ownership and mode check')
    def test_group_writable_archive_lock_is_rejected_before_publication(self):
        with self.fixture() as (root, token):
            storage = archive._archive_directory(root)
            lock = storage / 'archive.lock'
            lock.touch(mode=0o600)
            lock.chmod(0o660)
            with self.assertRaisesRegex(ValueError, 'unsafe ownership'):
                archive.create_recovery_archive(
                    root, ['state.json'], book_token=token, quota_bytes=100000)
            self.assertEqual(0o660, lock.stat().st_mode & 0o777)


    @unittest.skipIf(os.name == 'nt', 'Windows symlink creation may require privileges')
    def test_aliased_root_is_refused_before_state_read(self):
        with self.fixture() as (root, token), tempfile.TemporaryDirectory() as parent:
            alias = Path(parent) / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            with patch.object(archive, '_capture_book_token', side_effect=AssertionError('read through alias')):
                with self.assertRaisesRegex(ValueError, 'direct project directory'):
                    archive.create_recovery_archive(alias, ['state.json'], book_token=token, quota_bytes=100000)

    def test_invalid_archive_lock_directory_is_not_hidden_from_inventory(self):
        with self.fixture() as (root, _):
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            lock = storage / 'archive.lock'
            lock.mkdir()
            (lock / 'preserve').write_bytes(b'unknown')
            result = archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual(1, result['unrecognized_entries'])
            self.assertEqual(b'unknown', (lock / 'preserve').read_bytes())

    def test_extra_file_after_marker_results_in_uncertain_publication(self):
        with self.fixture() as (root, token):
            publish = archive._publish_marker
            def insert_after_marker(fd, identity, stage, digest):
                publish(fd, identity, stage, digest)
                (root / archive.ARCHIVE_DIR / stage / 'extra').write_bytes(b'unknown')
            with patch.object(archive, '_publish_marker', side_effect=insert_after_marker):
                with self.assertRaises(archive.ArchivePublicationUncertain) as error:
                    archive.create_recovery_archive(root, ['state.json'], book_token=token, quota_bytes=100000)
            self.assertTrue((root / archive.ARCHIVE_DIR / error.exception.recovery_id).exists())
            self.assertEqual(1, len(list((root / archive.ARCHIVE_DIR).glob('.pending-*/extra'))))

    def test_missing_commit_or_prepared_record_reports_uncertain_publication(self):
        for removed in ('commit', 'prepared'):
            with self.subTest(removed=removed), self.fixture() as (root, token):
                publish = archive._publish_marker
                def remove_after_publication(fd, identity, stage, digest):
                    publish(fd, identity, stage, digest)
                    name = identity if removed == 'commit' else 'prepared-' + identity
                    (root / archive.ARCHIVE_DIR / name).unlink()
                with patch.object(archive, '_publish_marker', side_effect=remove_after_publication):
                    with self.assertRaises(archive.ArchivePublicationUncertain):
                        archive.create_recovery_archive(root, ['state.json'], book_token=token,
                                                        quota_bytes=100000)

    @unittest.skipIf(os.name == 'nt', 'POSIX mode validation')
    def test_lock_exit_failure_after_marker_reports_uncertain_publication(self):
        with self.fixture() as (root, token):
            publish = archive._publish_marker
            storage = root / archive.ARCHIVE_DIR
            def weaken_storage(*args, **kwargs):
                result = publish(*args, **kwargs)
                storage.chmod(0o755)
                return result
            try:
                with patch.object(archive, '_publish_marker', side_effect=weaken_storage):
                    with self.assertRaises(archive.ArchivePublicationUncertain) as uncertain:
                        archive.create_recovery_archive(
                            root, ['state.json'], book_token=token, quota_bytes=100000)
                self.assertTrue((storage / uncertain.exception.recovery_id).exists())
            finally:
                if storage.exists():
                    storage.chmod(0o700)

    def test_extra_file_after_initial_verification_is_detected(self):
        import project_scope_files
        with self.fixture() as (root, token):
            receipt = archive.create_recovery_archive(root, ['state.json'], book_token=token, quota_bytes=100000)
            bundle = root / archive.ARCHIVE_DIR / receipt['stage']
            capture = project_scope_files.capture_scope_file
            def insert_on_second_manifest(directory, name, **kwargs):
                if name == receipt['stage'] + '/manifest.json' and not kwargs.get('include_bytes'):
                    (bundle / 'extra').write_bytes(b'unknown')
                return capture(directory, name, **kwargs)
            with patch.object(project_scope_files, 'capture_scope_file', side_effect=insert_on_second_manifest):
                with self.assertRaisesRegex(ValueError, 'unexpected'):
                    archive.verify_recovery_archive(root, receipt['id'],
                        expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=100000)
            self.assertEqual(b'unknown', (bundle / 'extra').read_bytes())

    def test_quota_reserves_manifest_and_receipts_before_copy(self):
        with self.fixture() as (root, token):
            name = 'annotated_script.json'
            info = (root / name).stat()
            identity, suffix, created = 'a' * 32, 'b' * 32, 123456789
            stage = '.pending-' + identity + '-' + suffix
            template = {'version':1, 'id':identity, 'book_token':token, 'created_ns':created,
                'files':[{'path':name, 'size':info.st_size, 'sha256':'0' * 64,
                          'source_version':list(archive._version(info)), 'mode':stat.S_IMODE(info.st_mode)}],
                'bytes':info.st_size}
            manifest_size = len(json.dumps(template, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode())
            required = info.st_size + manifest_size + 2 * len(archive._marker_bytes(identity, stage, '0' * 64))
            def ids():
                return [SimpleNamespace(hex=identity), SimpleNamespace(hex=suffix)]
            with patch.object(archive.uuid, 'uuid4', side_effect=ids()), \
                    patch.object(archive.time, 'time_ns', return_value=created), \
                    patch.object(archive, '_copy_artifact', side_effect=AssertionError('copied before reservation')):
                with self.assertRaisesRegex(ValueError, 'quota'):
                    archive.create_recovery_archive(root, [name], book_token=token, quota_bytes=required - 1)
            storage = root / archive.ARCHIVE_DIR
            self.assertFalse(any(path.name.startswith('.pending-') for path in storage.iterdir()))
            with patch.object(archive.uuid, 'uuid4', side_effect=ids()), \
                    patch.object(archive.time, 'time_ns', return_value=created):
                receipt = archive.create_recovery_archive(root, [name], book_token=token, quota_bytes=required)
            verified = archive.verify_recovery_archive(root, receipt['id'],
                expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=required)
            self.assertEqual(1, verified['files'])

    def test_observation_manifest_reservation_includes_revision_before_copy(self):
        from project_scope_files import capture_scope_file
        with self.fixture() as (root, token):
            name = 'annotated_script.json'
            info = (root / name).stat()
            proof = capture_scope_file(root, name, limit=100000)[0]
            identity, suffix, created, revision = 'a' * 32, 'b' * 32, 123456789, 'c' * 64
            stage = '.pending-' + identity + '-' + suffix
            template = {'version':2, 'id':identity, 'book_token':token, 'created_ns':created,
                'files':[{'path':name, 'size':info.st_size, 'sha256':'0' * 64,
                          'source_version':list(archive._version(info)), 'mode':stat.S_IMODE(info.st_mode)}],
                'bytes':info.st_size, 'observation_revision':revision, 'observation_complete':False}
            manifest_size = len(json.dumps(template, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode())
            required = info.st_size + manifest_size + 2 * len(archive._marker_bytes(identity, stage, '0' * 64))
            def ids():
                return [SimpleNamespace(hex=identity), SimpleNamespace(hex=suffix)]
            options = {'book_token':token, 'expected_proofs':[proof], 'observation_revision':revision,
                       '_prepublication_check':lambda: None}
            with patch.object(archive.uuid, 'uuid4', side_effect=ids()), \
                    patch.object(archive.time, 'time_ns', return_value=created), \
                    patch.object(archive, '_copy_artifact', side_effect=AssertionError('copied before reservation')):
                with self.assertRaisesRegex(ValueError, 'quota'):
                    archive.create_recovery_archive(root, [name], quota_bytes=required - 1, **options)
            self.assertFalse(any(path.name.startswith('.pending-') for path in (root / archive.ARCHIVE_DIR).iterdir()))
            with patch.object(archive.uuid, 'uuid4', side_effect=ids()), \
                    patch.object(archive.time, 'time_ns', return_value=created):
                receipt = archive.create_recovery_archive(root, [name], quota_bytes=required, **options)
            checked = archive.verify_recovery_archive(root, receipt['id'],
                expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=required)
            self.assertEqual(revision, checked['observation_revision'])
            self.assertIs(checked['observation_complete'], False)

    def test_invalid_paths_symlinks_and_hardlinks_are_not_captured(self):
        with self.fixture() as (root,token):
            for name in ('../outside','/absolute','audio/../state.json','audio//line.wav','.book_recovery/x','.BOOK_RECOVERY/x','C:\\other','file:stream','CON','NUL.txt','trailing.'):
                with self.subTest(name=name),self.assertRaises(ValueError):
                    archive.create_recovery_archive(root,[name],book_token=token,quota_bytes=10*1024**2)
            try:
                (root/'link').symlink_to(root/'state.json')
                os.link(root/'state.json',root/'hardlink')
            except (OSError,NotImplementedError):
                self.skipTest('Links unavailable on this host')
            for name in ('link','hardlink'):
                with self.subTest(name=name),self.assertRaises(ValueError):
                    archive.create_recovery_archive(root,[name],book_token=token,quota_bytes=10*1024**2)

    def test_source_changed_before_commit_aborts_without_deleting_source(self):
        with self.fixture() as (root,token):
            copied=archive._copy_artifact
            def change(*args,**kwargs):
                result=copied(*args,**kwargs)
                (root/'annotated_script.json').write_text('[{"text":"new user edit"}]')
                return result
            with patch.object(archive,'_copy_artifact',side_effect=change):
                with self.assertRaisesRegex(ValueError,'changed before publication'):
                    archive.create_recovery_archive(root,['annotated_script.json'],book_token=token,quota_bytes=100000)
            self.assertIn('new user edit',(root/'annotated_script.json').read_text())
            self.assertTrue(any(p.name.startswith('.pending-') for p in (root/archive.ARCHIVE_DIR).iterdir()))

    def test_untrusted_manifest_paths_duplicates_and_extra_payloads_are_rejected(self):
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            bundle=root/archive.ARCHIVE_DIR/receipt['stage']; manifest_path=bundle/'manifest.json'
            original=json.loads(manifest_path.read_text())
            for path in ('../state.json','.book_recovery/hidden','/tmp/a'):
                altered=json.loads(json.dumps(original)); altered['files'][0]['path']=path
                manifest_path.write_text(json.dumps(altered))
                with self.subTest(path=path),self.assertRaises(ValueError):
                    archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=self.rebind_test_marker(root,receipt),quota_bytes=100000)
            manifest_path.write_text(json.dumps(original));(bundle/'payload/extra').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError,'unexpected'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=self.rebind_test_marker(root,receipt),quota_bytes=100000)
            (bundle/'payload/extra').unlink()
            manifest_path.write_text('{"version":1,"version":1}')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=self.rebind_test_marker(root,receipt),quota_bytes=100000)

    def test_verifier_rechecks_earlier_payloads_after_later_reads(self):
        import project_scope_files
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json','annotated_script.json'],
                book_token=token,quota_bytes=100000)
            bundle=root/archive.ARCHIVE_DIR/receipt['stage']; capture=project_scope_files.capture_scope_file
            def mutate(directory,name,*args,**kwargs):
                if name.endswith('/payload/1'):
                    (bundle/'payload/0').write_bytes(b'changed after first read')
                return capture(directory,name,*args,**kwargs)
            with patch.object(project_scope_files,'capture_scope_file',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'changed during verification'):
                    archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=100000)

    def test_verifier_rechecks_payload_after_receipt_read(self):
        import project_scope_files
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            bundle=root/archive.ARCHIVE_DIR/receipt['stage']; capture=project_scope_files.capture_scope_file
            calls=0
            def mutate(directory,name,*args,**kwargs):
                nonlocal calls
                result=capture(directory,name,*args,**kwargs)
                if name=='prepared-'+receipt['id']:
                    calls+=1
                    if calls==2: (bundle/'payload/0').write_bytes(b'changed after receipt read')
                return result
            with patch.object(project_scope_files,'capture_scope_file',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'changed during verification'):
                    archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=100000)

    def test_manifest_and_payload_changed_together_do_not_match_recorded_identity(self):
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            bundle=root/archive.ARCHIVE_DIR/receipt['stage']; manifest=bundle/'manifest.json'
            payload=bundle/'payload/0'; replacement=b'x'*payload.stat().st_size; payload.write_bytes(replacement)
            data=json.loads(manifest.read_text()); data['files'][0]['sha256']=hashlib.sha256(replacement).hexdigest()
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'recorded identity'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=100000)

    def test_unexpected_bundle_root_and_storage_quota_are_checked(self):
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            extra=root/archive.ARCHIVE_DIR/receipt['stage']/'unexpected'; extra.write_bytes(b'x')
            with self.assertRaisesRegex(ValueError,'unexpected'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=100000)
            extra.write_bytes(b'x'*200000)
            with self.assertRaisesRegex(ValueError,'quota'):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],quota_bytes=100000)

    def test_duplicate_and_ancestor_targets_are_refused_before_capture(self):
        with self.fixture() as (root,token):
            for names in (['foo','foo/bar'],['foo/bar','foo'],['foo','foo bar','foo/bar'],['State.json','state.json'],['å','a\u030a']):
                with self.subTest(names=names),self.assertRaisesRegex(ValueError,'conflicting'):
                    archive.create_recovery_archive(root,names,book_token=token,quota_bytes=100000)
            receipt=archive.create_recovery_archive(root,['state.json','annotated_script.json'],book_token=token,quota_bytes=100000)
            manifest=root/archive.ARCHIVE_DIR/receipt['stage']/'manifest.json'; original=json.loads(manifest.read_text())
            for names in (('foo','foo/bar'),('foo/bar','foo'),('NAME','name')):
                data=json.loads(json.dumps(original))
                for row,name in zip(data['files'],names):row['path']=name
                manifest.write_text(json.dumps(data))
                with self.subTest(manifest_names=names),self.assertRaisesRegex(ValueError,'conflicting'):
                    archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=self.rebind_test_marker(root,receipt),quota_bytes=100000)

    def test_cancel_before_publish_leaves_only_pending_data(self):
        from audio_generation_owner import AudioGenerationCancelled
        with self.fixture() as (root,token):
            cancelled=False; sync=archive._sync_owned_directory
            def cancel_at_sync(fd):
                nonlocal cancelled
                sync(fd); cancelled=True
            with patch.object(archive,'_sync_owned_directory',side_effect=cancel_at_sync):
                with self.assertRaises(AudioGenerationCancelled):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000,
                                                     cancel_check=lambda:cancelled)
            entries=list((root/archive.ARCHIVE_DIR).iterdir())
            self.assertTrue(any(p.name.startswith('.pending-') for p in entries))
            self.assertFalse(any(archive._ID.fullmatch(p.name) for p in entries))

    def test_cancel_is_checked_before_and_during_quota_walk(self):
        from audio_generation_owner import AudioGenerationCancelled
        with self.fixture() as (root,token):
            with self.assertRaises(AudioGenerationCancelled):
                archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000,
                                                 cancel_check=lambda:True)
            self.assertFalse((root/archive.ARCHIVE_DIR).exists())
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            checks=0
            def cancel_while_walking():
                nonlocal checks
                checks+=1
                return checks>=3
            with self.assertRaises(AudioGenerationCancelled):
                archive.verify_recovery_archive(root,receipt['id'],expected_manifest_sha256=receipt['manifest_sha256'],
                                                quota_bytes=100000,cancel_check=cancel_while_walking)
            self.assertEqual(3,checks)

    def test_substituted_stage_is_never_deleted_on_failure(self):
        with self.fixture() as (root,token):
            def substitute(_root,_name,destination,*_args,**_kwargs):
                stage=destination.parent.parent; os.rename(stage,stage.with_name(stage.name+'-moved'))
                stage.mkdir(); (stage/'important').write_text('preserve')
                raise OSError('substituted')
            with patch.object(archive,'_copy_artifact',side_effect=substitute):
                with self.assertRaisesRegex(OSError,'substituted'):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            self.assertEqual(['preserve'],[p.read_text() for p in (root/archive.ARCHIVE_DIR).glob('.pending-*/important')])

    def test_stage_symlink_substitution_cannot_copy_into_an_outside_directory(self):
        with self.fixture() as (root,token), tempfile.TemporaryDirectory() as outside:
            copied=archive._copy_artifact; outside=Path(outside); (outside/'payload').mkdir()
            def substitute(_root,_name,destination,*args,**kwargs):
                stage=destination.parent.parent; os.rename(stage,stage.with_name(stage.name+'-moved'))
                stage.symlink_to(outside,target_is_directory=True)
                return copied(_root,_name,destination,*args,**kwargs)
            with patch.object(archive,'_copy_artifact',side_effect=substitute):
                with self.assertRaises(ValueError):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            self.assertFalse((outside/'payload/0').exists())

    def test_commit_marker_never_overwrites_an_existing_target(self):
        with self.fixture() as (root,token):
            publish=archive._publish_marker
            def conflict(fd,recovery_id,*args):
                target=root/archive.ARCHIVE_DIR/recovery_id; target.mkdir(); (target/'important').write_text('keep')
                return publish(fd,recovery_id,*args)
            with patch.object(archive,'_publish_marker',side_effect=conflict):
                with self.assertRaisesRegex(ValueError,'already exists'):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            self.assertEqual(['keep'],[p.read_text() for p in (root/archive.ARCHIVE_DIR).glob('*/important')])

    def test_failure_after_marker_creation_preserves_a_recoverable_receipt(self):
        with self.fixture() as (root,token):
            sync=archive._sync_owned_directory; count=0
            def fail_final(fd):
                nonlocal count
                count+=1
                if count==4: raise OSError('directory sync failed')
                sync(fd)
            with patch.object(archive,'_sync_owned_directory',side_effect=fail_final):
                with self.assertRaises(archive.ArchivePublicationUncertain) as uncertain:
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            result=archive.verify_recovery_archive(root,uncertain.exception.recovery_id,
                expected_manifest_sha256=uncertain.exception.manifest_sha256,quota_bytes=100000)
            self.assertEqual(token,result['book_token'])

    def test_read_only_inventory_distinguishes_committed_and_interrupted_stages(self):
        with self.fixture() as (root,token):
            committed=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            with patch.object(archive,'_publish_marker',side_effect=OSError('interrupted')):
                with self.assertRaisesRegex(OSError,'interrupted'):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            storage=root/archive.ARCHIVE_DIR;(storage/'unknown').write_bytes(b'unknown')
            before={str(p.relative_to(storage)):p.read_bytes() for p in storage.rglob('*') if p.is_file()}
            listing=archive.list_recovery_records(root,quota_bytes=100000)
            self.assertEqual({'committed_unverified','prepared_without_marker'},
                             {row['status'] for row in listing['records']})
            self.assertEqual(1,listing['unrecognized_entries'])
            item=next(row for row in listing['records'] if row['id']==committed['id'])
            self.assertEqual(committed['manifest_sha256'],item['manifest_sha256'])
            self.assertEqual(before,{str(p.relative_to(storage)):p.read_bytes() for p in storage.rglob('*') if p.is_file()})
            self.assertTrue(archive.list_recovery_records(root,quota_bytes=1)['over_quota'])

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink and FIFO fixtures required')
    def test_inventory_retains_unsafe_neighbors_and_reports_valid_candidate(self):
        for unsafe_kind in ('symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=unsafe_kind), self.fixture() as (root, token):
                receipt = archive.create_recovery_archive(
                    root, ['state.json'], book_token=token, quota_bytes=100000)
                storage = root / archive.ARCHIVE_DIR
                foreign = root / 'foreign-evidence'
                foreign.write_bytes(b'foreign evidence must remain untouched')
                unsafe = storage / 'unknown'
                if unsafe_kind == 'symlink':
                    unsafe.symlink_to(foreign)
                elif unsafe_kind == 'hardlink':
                    os.link(foreign, unsafe)
                else:
                    os.mkfifo(unsafe)
                before = unsafe.lstat()
                listing = archive.list_recovery_records(root, quota_bytes=100000)
                self.assertEqual(1, listing['unrecognized_entries'])
                self.assertEqual(1, listing['unsafe_entries'])
                self.assertFalse(listing['storage_complete'])
                self.assertIsNone(listing['over_quota'])
                self.assertEqual([receipt['id']], [row['id'] for row in listing['records']])
                self.assertEqual('committed_unverified', listing['records'][0]['status'])
                self.assertEqual(b'foreign evidence must remain untouched', foreign.read_bytes())
                after = unsafe.lstat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode),
                                 (after.st_dev, after.st_ino, after.st_mode))
                with self.assertRaises(ValueError):
                    archive._used_bytes(storage)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture required')
    def test_inventory_marks_nested_unsafe_candidate_as_needing_inspection(self):
        with self.fixture() as (root, token):
            receipt = archive.create_recovery_archive(
                root, ['state.json'], book_token=token, quota_bytes=100000)
            storage = root / archive.ARCHIVE_DIR
            foreign = root / 'foreign-evidence'
            foreign.write_bytes(b'untouched foreign bytes')
            (storage / receipt['stage'] / 'payload' / 'foreign').symlink_to(foreign)
            listing = archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual('needs_inspection', listing['records'][0]['status'])
            self.assertEqual(1, listing['unsafe_entries'])
            self.assertFalse(listing['storage_complete'])
            self.assertEqual(b'untouched foreign bytes', foreign.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture required')
    def test_inventory_rejects_unsafe_leaf_substitution_after_scan(self):
        with self.fixture() as (root, token):
            archive.create_recovery_archive(
                root, ['state.json'], book_token=token, quota_bytes=100000)
            storage = root / archive.ARCHIVE_DIR
            unsafe = storage / 'unknown'
            unsafe.symlink_to(root / 'state.json')
            original = archive._list_recovery_records_guarded
            def substitute(*args, **kwargs):
                unsafe.unlink()
                unsafe.write_bytes(b'replacement')
                return original(*args, **kwargs)
            with patch.object(archive, '_list_recovery_records_guarded', substitute):
                with self.assertRaisesRegex(ValueError, 'changed during inspection'):
                    archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual(b'replacement', unsafe.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX directory descriptors required')
    def test_listing_parent_swap_never_stats_the_external_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            nested = storage / 'nested'
            nested.mkdir(mode=0o700)
            (nested / 'unsafe').symlink_to(root / 'missing')
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'unsafe').symlink_to(root / 'other-missing')
            admitted = nested.stat()
            expected = (admitted.st_dev, admitted.st_ino)
            original_scan, original_stat = os.scandir, os.stat
            swapped, parents = [], []
            @contextlib.contextmanager
            def swap_after_enumeration(directory):
                with original_scan(directory) as entries:
                    info = os.fstat(directory) if isinstance(directory, int) else None
                    if info is not None and (info.st_dev, info.st_ino) == expected and not swapped:
                        entries = list(entries)
                        nested.rename(storage / 'parked')
                        nested.symlink_to(outside, target_is_directory=True)
                        swapped.append(True)
                    yield entries
            def inspect(name, *args, **kwargs):
                if name == 'unsafe' and swapped and kwargs.get('dir_fd') is not None:
                    info = os.fstat(kwargs['dir_fd'])
                    parents.append((info.st_dev, info.st_ino))
                return original_stat(name, *args, **kwargs)
            try:
                with patch.object(archive.os, 'scandir', swap_after_enumeration), \
                     patch.object(archive.os, 'stat', inspect), \
                     patch.object(archive.os, 'supports_fd', set(os.supports_fd) | {swap_after_enumeration}), \
                     patch.object(archive.os, 'supports_dir_fd', set(os.supports_dir_fd) | {inspect}), \
                     patch.object(archive.os, 'supports_follow_symlinks', set(os.supports_follow_symlinks) | {inspect}):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        archive.list_recovery_records(root, quota_bytes=10000)
                self.assertEqual([True], swapped)
                self.assertTrue(parents)
                self.assertTrue(all(identity == expected for identity in parents))
                self.assertEqual(str(root / 'other-missing'), os.readlink(outside / 'unsafe'))
            finally:
                if swapped:
                    nested.unlink()

    def test_inventory_reports_nonprivate_stage_and_lock_as_unsafe(self):
        for stage in (False, True):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                storage = root / archive.ARCHIVE_DIR
                storage.mkdir(mode=0o700)
                name = '.pending-' + 'a' * 32 + '-' + 'b' * 32 if stage else 'archive.lock'
                target = storage / name
                if stage:
                    target.mkdir(mode=0o700)
                else:
                    target.write_bytes(b'lock evidence')
                os.chmod(target, 0o755 if stage else 0o660)
                listing = archive.list_recovery_records(root, quota_bytes=10000)
                self.assertEqual(1, listing['unsafe_entries'])
                self.assertFalse(listing['storage_complete'])
                if stage:
                    self.assertEqual('needs_inspection', listing['records'][0]['status'])
                else:
                    self.assertEqual(1, listing['unrecognized_entries'])

    def test_inventory_refuses_hardlink_added_after_scan(self):
        with self.fixture() as (root, token):
            receipt = archive.create_recovery_archive(
                root, ['state.json'], book_token=token, quota_bytes=100000)
            payload = root / archive.ARCHIVE_DIR / receipt['stage'] / 'payload' / '0'
            foreign = root / 'foreign-link'
            original = archive._used_bytes
            def add_link(*args, **kwargs):
                result = original(*args, **kwargs)
                os.link(payload, foreign)
                return result
            with patch.object(archive, '_used_bytes', add_link):
                with self.assertRaisesRegex(ValueError, 'changed during inspection'):
                    archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual(payload.read_bytes(), foreign.read_bytes())

    def test_listing_parent_preserves_io_and_cancellation_errors(self):
        class CancelledIO(OSError):
            pass
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            before = archive._version(directory.stat())
            for error in (OSError(errno.EIO, 'synthetic read failure'),
                          OSError(errno.EMFILE, 'synthetic descriptor exhaustion'),
                          CancelledIO(errno.EINTR, 'synthetic cancellation')):
                with self.subTest(errno=error.errno):
                    with self.assertRaises(OSError) as raised:
                        with archive._listing_parent(directory, '', before, {}):
                            raise error
                    self.assertIs(error, raised.exception)
            error = OSError(errno.EIO, 'synthetic scanner failure')
            with patch.object(archive, '_used_bytes_pinned', side_effect=error):
                with self.assertRaises(OSError) as raised:
                    archive._used_bytes(directory, with_proofs=True, observe_unsafe=True)
            self.assertIs(error, raised.exception)

    def test_inventory_reuses_pinned_root_during_proof_recheck(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            original = archive._open_directory
            totals = []
            for count in (20, 200):
                for number in range(count):
                    path = storage / str(number)
                    path.write_bytes(b'known')
                    path.chmod(0o600)
                anchors = []
                def opened(path, **kwargs):
                    if str(path) == storage.anchor and kwargs.get('dir_fd') is None:
                        anchors.append(True)
                    return original(path, **kwargs)
                with patch.object(archive, '_open_directory', opened):
                    result = archive.list_recovery_records(root, quota_bytes=10000)
                self.assertEqual(count, result['unrecognized_entries'])
                self.assertTrue(result['storage_complete'])
                self.assertEqual(0, result['unsafe_entries'])
                self.assertFalse(result['over_quota'])
                self.assertLessEqual(len(anchors), 5)
                totals.append(len(anchors))
            self.assertEqual(totals[0], totals[1])

    def test_listing_refuses_missing_descriptor_capability_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            sentinel = storage / 'evidence'
            sentinel.write_bytes(b'preserved')
            with patch.object(archive.os, 'supports_fd', set(os.supports_fd) - {os.scandir}):
                with self.assertRaisesRegex(ValueError, 'Secure recovery listing is unavailable'):
                    archive.list_recovery_records(root, quota_bytes=10000)
            self.assertEqual(b'preserved', sentinel.read_bytes())

    def test_listing_refuses_leaf_removed_after_parent_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            leaf = directory / 'leaf'
            leaf.write_bytes(b'owned')
            before = archive._version(directory.stat())
            original = os.stat
            removed = []
            def disappear(name, *args, **kwargs):
                if name == 'leaf' and kwargs.get('dir_fd') is not None:
                    leaf.unlink()
                    removed.append(True)
                return original(name, *args, **kwargs)
            with patch.object(archive.os, 'stat', disappear):
                with self.assertRaisesRegex(ValueError, 'changed during inspection') as raised:
                    archive._listing_stat(directory, 'leaf', before, {})
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
            self.assertEqual([True], removed)

    @unittest.skipIf(os.name == 'nt', 'POSIX no-follow directory open required')
    def test_listing_refuses_directory_replaced_before_pinned_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            child = storage / 'child'
            child.mkdir(mode=0o700)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'sentinel').write_bytes(b'foreign')
            original = archive._open_directory
            swapped = []
            def replace(name, **kwargs):
                if name == 'child' and kwargs.get('dir_fd') is not None and not swapped:
                    child.rename(storage / 'parked')
                    child.symlink_to(outside, target_is_directory=True)
                    swapped.append(True)
                return original(name, **kwargs)
            try:
                with patch.object(archive, '_open_directory', replace):
                    with self.assertRaisesRegex(ValueError, 'changed during quota scan'):
                        archive.list_recovery_records(root, quota_bytes=10000)
                self.assertEqual([True], swapped)
                self.assertEqual(b'foreign', (outside / 'sentinel').read_bytes())
            finally:
                if swapped:
                    child.unlink()

    def test_inventory_counts_hardlink_logical_bytes_per_name(self):
        with self.fixture() as (root, token):
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            foreign = root / 'foreign'
            foreign.write_bytes(b'x' * 2048)
            os.link(foreign, storage / 'one')
            os.link(foreign, storage / 'two')
            listing = archive.list_recovery_records(root, quota_bytes=100)
            self.assertEqual(4096, listing['storage_bytes'])
            self.assertEqual(2, listing['unsafe_entries'])
            self.assertFalse(listing['storage_complete'])
            self.assertTrue(listing['over_quota'])
            self.assertEqual(b'x' * 2048, foreign.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture required')
    def test_inventory_unsafe_neighbor_keeps_pagination_revision_fence(self):
        with self.fixture() as (root, token):
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            for number in range(2):
                (storage / ('.pending-' + f'{number:032x}' + '-' + 'f' * 32)).mkdir(mode=0o700)
            unsafe = storage / 'unknown'
            unsafe.symlink_to(root / 'state.json')
            first = archive.list_recovery_records(root, quota_bytes=10000, limit=1)
            second = archive.list_recovery_records(root, quota_bytes=10000, limit=1,
                cursor=first['next_cursor'], expected_revision=first['revision'])
            self.assertEqual(first['revision'], second['revision'])
            self.assertNotEqual(first['records'][0]['id'], second['records'][0]['id'])
            self.assertIsNone(second['next_cursor'])
            for page in (first, second):
                self.assertFalse(page['storage_complete'])
                self.assertEqual(1, page['unsafe_entries'])
                self.assertIsNone(page['over_quota'])
            unsafe.unlink()
            unsafe.symlink_to(root / 'annotated_script.json')
            with self.assertRaisesRegex(ValueError, 'changed; start listing again'):
                archive.list_recovery_records(root, quota_bytes=10000, limit=1,
                    cursor=first['next_cursor'], expected_revision=first['revision'])

    def test_inventory_retains_owner_unusable_directory_and_looping_links(self):
        with self.fixture() as (root, token):
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            child = storage / 'unreadable'
            child.mkdir(mode=0o700)
            (storage / 'dangling').symlink_to(root / 'missing')
            (storage / 'loop').symlink_to('loop')
            try:
                for mode in (0o000, 0o100, 0o400, 0o600):
                    child.chmod(mode)
                    listing = archive.list_recovery_records(root, quota_bytes=100000)
                    self.assertEqual(3, listing['unsafe_entries'])
                    self.assertFalse(listing['storage_complete'])
                    self.assertIsNone(listing['over_quota'])
                    self.assertEqual('loop', os.readlink(storage / 'loop'))
            finally:
                child.chmod(0o700)

    def test_inventory_rejects_new_top_level_name_when_root_timestamps_match(self):
        with self.fixture() as (root, token):
            storage = root / archive.ARCHIVE_DIR
            storage.mkdir(mode=0o700)
            original_scan, original_version = archive._used_bytes, archive._version
            info = storage.stat()
            identity, version = (info.st_dev, info.st_ino), original_version(info)
            added = storage / ('prepared-' + 'a' * 32)
            def scan_then_add(*args, **kwargs):
                result = original_scan(*args, **kwargs)
                added.write_bytes(b'concurrent record retained')
                added.chmod(0o600)
                return result
            def coarse_root_version(info):
                return version if (info.st_dev, info.st_ino) == identity else original_version(info)
            with patch.object(archive, '_used_bytes', scan_then_add), \
                 patch.object(archive, '_version', coarse_root_version):
                with self.assertRaisesRegex(ValueError, 'changed; start listing again'):
                    archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual(b'concurrent record retained', added.read_bytes())

    def test_unsafe_observation_requires_path_proofs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, 'require listing proofs'):
                archive._used_bytes(directory, observe_unsafe=True)
            descriptor = archive._open_directory(str(directory))
            try:
                with self.assertRaisesRegex(ValueError, 'require listing proofs'):
                    archive._used_bytes(directory, observe_unsafe=True, with_proofs=True,
                                        directory_fd=descriptor)
            finally:
                os.close(descriptor)

    def test_empty_inventory_does_not_create_recovery_storage(self):
        with self.fixture() as (root,token):
            listing=archive.list_recovery_records(root,quota_bytes=100000)
            self.assertEqual([],listing['records'])
            self.assertEqual(0,listing['storage_bytes'])
            self.assertFalse((root/archive.ARCHIVE_DIR).exists())

    def test_inventory_reports_incomplete_marker_without_treating_it_as_committed(self):
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            marker=root/archive.ARCHIVE_DIR/receipt['id'];marker.write_bytes(b'')
            listing=archive.list_recovery_records(root,quota_bytes=100000)
            self.assertEqual('incomplete_marker',listing['records'][0]['status'])
            self.assertEqual(b'',marker.read_bytes())

    def test_inventory_does_not_report_an_oversized_marker_as_missing(self):
        with self.fixture() as (root, token):
            receipt = archive.create_recovery_archive(root, ['state.json'], book_token=token, quota_bytes=100000)
            marker = root / archive.ARCHIVE_DIR / receipt['id']
            marker.write_bytes(b'x' * 1025)
            listing = archive.list_recovery_records(root, quota_bytes=100000)
            self.assertEqual('needs_inspection', listing['records'][0]['status'])
            self.assertEqual(1025, marker.stat().st_size)

    def test_inventory_does_not_classify_wrong_object_types_as_valid_stages(self):
        with self.fixture() as (root,token):
            storage=archive._archive_directory(root); identity='a'*32
            stage_name='.pending-'+identity+'-'+'b'*32
            (storage/stage_name).write_bytes(b'not a directory')
            record=archive._marker_bytes(identity,stage_name,'c'*64)
            for name in (identity,'prepared-'+identity):
                path=storage/name; path.write_bytes(record); os.chmod(path,0o600)
            listing=archive.list_recovery_records(root,quota_bytes=100000)
            self.assertEqual('needs_inspection',listing['records'][0]['status'])
            self.assertEqual(b'not a directory',(storage/stage_name).read_bytes())

    def test_inventory_paginates_more_than_256_candidates_and_fences_stale_pages(self):
        with self.fixture() as (root,token):
            for _ in range(3):
                archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=10*1024**2)
            storage=root/archive.ARCHIVE_DIR
            for number in range(257):
                (storage/('.pending-'+f'{number:032x}'+'-'+'f'*32)).mkdir(mode=0o700)
            records=[]; cursor=None; revision=None
            while True:
                page=archive.list_recovery_records(root,quota_bytes=10*1024**2,limit=50,
                    cursor=cursor,expected_revision=revision)
                if revision is None:revision=page['revision']
                self.assertEqual(260,page['total_candidates'])
                records.extend(page['records']); cursor=page['next_cursor']
                if cursor is None:break
            self.assertEqual(260,len(records));self.assertEqual(260,len({row['id'] for row in records}))
            self.assertEqual(3,sum(row['status']=='committed_unverified' for row in records))
            with self.assertRaisesRegex(ValueError, 'requires the prior inventory revision'):
                archive.list_recovery_records(root, quota_bytes=10*1024**2, cursor=records[0]['id'])
            (storage/('.pending-'+'e'*32+'-'+'f'*32)).mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError,'changed; start listing again'):
                archive.list_recovery_records(root,quota_bytes=10*1024**2,limit=50,expected_revision=revision)

    def test_inventory_rejects_nested_growth_during_quota_observation(self):
        with self.fixture() as (root,token):
            receipt=archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            payload=root/archive.ARCHIVE_DIR/receipt['stage']/'payload/0'; walk=archive._used_bytes
            def grow(*args,**kwargs):
                result=walk(*args,**kwargs);payload.write_bytes(b'x'*5000);return result
            with patch.object(archive,'_used_bytes',side_effect=grow):
                with self.assertRaisesRegex(ValueError,'changed during inspection'):
                    archive.list_recovery_records(root,quota_bytes=100000)

    def test_changed_staged_payload_is_not_published(self):
        with self.fixture() as (root,token):
            sync=archive._sync_owned_directory; calls=0
            def tamper(fd):
                nonlocal calls
                sync(fd); calls+=1
                if calls==1:
                    next((root/archive.ARCHIVE_DIR).glob('.pending-*/payload/0')).write_bytes(b'wrong')
            with patch.object(archive,'_sync_owned_directory',side_effect=tamper):
                with self.assertRaisesRegex(ValueError,'staging payload changed'):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            entries=list((root/archive.ARCHIVE_DIR).iterdir())
            self.assertFalse(any(archive._ID.fullmatch(p.name) for p in entries))
            self.assertTrue(any(p.name.startswith('.pending-') for p in entries))

    def test_changed_manifest_after_verification_is_not_published(self):
        with self.fixture() as (root,token):
            token_fn=archive._capture_book_token; calls=0
            def tamper(path, cancel_check):
                nonlocal calls
                calls+=1
                if calls==2:
                    manifest=next((root/archive.ARCHIVE_DIR).glob('.pending-*/manifest.json'))
                    manifest.write_text('{"changed":true}')
                return token_fn(path, cancel_check)
            with patch.object(archive,'_capture_book_token',side_effect=tamper):
                with self.assertRaisesRegex(ValueError,'staging manifest changed'):
                    archive.create_recovery_archive(root,['state.json'],book_token=token,quota_bytes=100000)
            self.assertFalse(any(archive._ID.fullmatch(p.name) for p in (root/archive.ARCHIVE_DIR).iterdir()))

    def test_process_death_before_publication_leaves_no_complete_archive(self):
        worker='''import os,sys
import core,book_recovery_archive as a
from book_state_transaction import get_book_operation_token
from project_scope_admission import ensure_scope_admission
root=sys.argv[1];core.DATA_DIR=root
with ensure_scope_admission(root):
 a._publish_marker=lambda *args: os._exit(77)
 a.create_recovery_archive(root,['state.json','annotated_script.json'],book_token=get_book_operation_token(root),quota_bytes=100000)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'state.json').write_text(
                '{"active_book_id":"A","book_generation":"one"}')
            (root/'annotated_script.json').write_text('[{"text":"A"}]')
            env={**os.environ,'ALEXANDRIA_DATA_DIR':str(root)}
            result=subprocess.run([sys.executable,'-c',worker,str(root)],capture_output=True,timeout=20,env=env)
            self.assertEqual(77,result.returncode,result.stderr.decode())
            storage=root/archive.ARCHIVE_DIR
            self.assertFalse(any(archive._ID.fullmatch(p.name) for p in storage.iterdir()))
            self.assertTrue(any(p.name.startswith('.pending-') for p in storage.iterdir()))
            prepared=next(storage.glob('prepared-*')); recovery_id=prepared.name.removeprefix('prepared-')
            self.assertEqual('missing',archive.inspect_prepared_recovery(root,recovery_id)['marker_status'])
            self.assertEqual('[{"text":"A"}]',(root/'annotated_script.json').read_text())

    def test_process_death_after_empty_marker_retains_prepared_receipt_for_inspection(self):
        worker='''import os,sys
import core,book_recovery_archive as a
from book_state_transaction import get_book_operation_token
from project_scope_admission import ensure_scope_admission
root=sys.argv[1];core.DATA_DIR=root
def die(fd,recovery_id,*args):
 os.open(recovery_id,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
 os._exit(78)
with ensure_scope_admission(root):
 a._publish_marker=die
 a.create_recovery_archive(root,['state.json','annotated_script.json'],book_token=get_book_operation_token(root),quota_bytes=100000)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'state.json').write_text(
                '{"active_book_id":"A","book_generation":"one"}')
            (root/'annotated_script.json').write_text('[{"text":"A"}]')
            env={**os.environ,'ALEXANDRIA_DATA_DIR':str(root)}
            result=subprocess.run([sys.executable,'-c',worker,str(root)],capture_output=True,timeout=20,env=env)
            self.assertEqual(78,result.returncode,result.stderr.decode())
            storage=root/archive.ARCHIVE_DIR;prepared=next(storage.glob('prepared-*'))
            recovery_id=prepared.name.removeprefix('prepared-')
            inspection=archive.inspect_prepared_recovery(root,recovery_id)
            self.assertEqual('incomplete',inspection['marker_status'])
            self.assertEqual(64,len(inspection['manifest_sha256']))
            with self.assertRaises(ValueError):
                archive.verify_recovery_archive(root,recovery_id,expected_manifest_sha256=inspection['manifest_sha256'],quota_bytes=100000)
            self.assertEqual('[{"text":"A"}]',(root/'annotated_script.json').read_text())
