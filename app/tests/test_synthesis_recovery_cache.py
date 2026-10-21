"""Real filesystem, admission and interrupted-index recovery tests."""
from contextlib import contextmanager
import copy
import hashlib
import math
import json
import os
from pathlib import Path
import subprocess
import sys
import struct
import wave
import tempfile
import threading
import unittest
from unittest.mock import patch

import synthesis_recovery_cache as cache
from utils import file_lock


@unittest.skipUnless(cache.is_recovery_storage_platform_supported(), 'Linux durability required')
class RecoveryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.depth = 0
        self.entries = 0

    @contextmanager
    def admit(self):
        self.entries += 1
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def open_cache(self):
        return cache.RecoveryCacheStorage(self.root, self.admit)

    def body(self):
        return {'version': 1, 'requests': {'a' * 64: {'uid': 'b' * 64, 'windows': {
            '0': {'state': 'reserved', 'attempt': 'c' * 32, 'claim': 'd' * 64,
                  'size': 1024, 'sha256': 'e' * 64, 'frames': 256,
                  'rate': 24000, 'channels': 1}}}}}

    def index(self):
        return self.root / cache.CACHE_DIRECTORY / cache.INDEX_NAME

    def save(self, storage, body):
        with storage.write_transaction() as transaction:
            transaction.data.clear()
            transaction.data.update(copy.deepcopy(body))
            transaction.commit()

    def test_restart_private_reads_and_persistent_lock_inode(self):
        with self.open_cache() as storage:
            self.save(storage, self.body())
            inode = self.index().with_suffix('.json.lock').stat().st_ino
        with self.open_cache() as storage:
            entries = self.entries
            with storage.read_transaction() as transaction:
                self.assertEqual(self.body(), transaction.data)
                transaction.data['requests'].clear()
            with storage.read_transaction() as transaction:
                self.assertEqual(self.body(), transaction.data)
            self.assertEqual(entries, self.entries)
            self.assertEqual(inode, self.index().with_suffix('.json.lock').stat().st_ino)

    def test_writer_owner_and_reader_closed_repeated_cross_thread_admission(self):
        with self.open_cache() as storage:
            real_save = storage._save_index
            def checked_save(body):
                self.assertEqual(1, self.depth)
                return real_save(body)
            with patch.object(storage, '_save_index', side_effect=checked_save):
                self.save(storage, self.body())
            with storage.read_transaction() as transaction:
                with self.assertRaises(cache.RecoveryCacheUnavailable):
                    transaction.commit()
            with self.assertRaises(cache.RecoveryCacheUnavailable):
                transaction.commit()
            with storage.write_transaction() as transaction:
                errors = []
                def wrong_thread():
                    try:
                        transaction.commit()
                    except Exception as error:
                        errors.append(error)
                worker = threading.Thread(target=wrong_thread)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(errors[0], cache.RecoveryCacheUnavailable)
                transaction.commit()
                with self.assertRaises(cache.RecoveryCacheUnavailable):
                    transaction.commit()

    def test_owner_cancellation_propagates_without_mutation(self):
        with self.open_cache():
            pass
        before = self.index().read_bytes()
        @contextmanager
        def cancelled():
            raise LookupError('stale task')
            yield
        with self.assertRaisesRegex(LookupError, 'stale task'):
            with cache.RecoveryCacheStorage(self.root, cancelled):
                pass
        self.assertEqual(before, self.index().read_bytes())

    def test_contention_is_nonblocking_and_releases_owner(self):
        with self.open_cache() as storage:
            with file_lock(str(self.index()), timeout=0):
                with patch('utils.time.sleep', side_effect=AssertionError('must not wait')):
                    with self.assertRaises(TimeoutError):
                        with storage.write_transaction():
                            self.fail('another descriptor owns the lock')
                self.assertEqual(0, self.depth)
            self.save(storage, self.body())

    def test_corrupt_missing_or_unknown_index_never_resets(self):
        with self.open_cache():
            pass
        for payload in (b'', b'{', b'[]', b'{"version":1,"version":1,"requests":{},"sha256":"x"}',
                        json.dumps({'version': 1, 'requests': {}, 'sha256': '0' * 64}).encode()):
            with self.subTest(payload=payload):
                self.index().write_bytes(payload)
                with self.assertRaises(cache.RecoveryCacheUnavailable):
                    with self.open_cache():
                        pass
                self.assertEqual(payload, self.index().read_bytes())
        self.index().unlink()
        with self.assertRaises(FileNotFoundError):
            with self.open_cache():
                pass
        self.assertFalse(self.index().exists())

    def test_pending_is_never_adopted(self):
        with self.open_cache() as storage:
            self.save(storage, self.body())
        pending = self.index().with_name(cache.PENDING_NAME)
        pending.write_bytes(b'partial proposal')
        pending.chmod(0o600)
        with self.open_cache() as storage:
            with storage.read_transaction() as transaction:
                self.assertEqual(self.body(), transaction.data)
            self.save(storage, {'version': 1, 'requests': {}})
        self.assertFalse(pending.exists())

    def test_invalid_schema_and_limits_preserve_committed_authority(self):
        variants = []
        for field, values in {'size': (True, 0, -1, cache.MAX_ARTIFACT_BYTES + 1, '1024'),
                              'state': ('ready',), 'attempt': ('../escape',), 'claim': (None,),
                              'rate': (True,), 'channels': (3,), 'frames': (0,), 'sha256': ('z' * 64,)}.items():
            for value in values:
                body = self.body()
                body['requests']['a' * 64]['windows']['0'][field] = value
                variants.append(body)
        body = self.body()
        body['requests']['a' * 64]['windows']['00'] = body['requests']['a' * 64]['windows'].pop('0')
        variants.append(body)
        body = self.body()
        body['requests']['a' * 64]['windows']['1'] = copy.deepcopy(body['requests']['a' * 64]['windows']['0'])
        variants.append(body)
        with self.open_cache() as storage:
            before = self.index().read_bytes()
            for body in variants:
                with self.subTest(body=body), self.assertRaises(cache.RecoveryCacheUnavailable):
                    self.save(storage, body)
                self.assertEqual(before, self.index().read_bytes())
            for name, value in (('MAX_INDEX_BYTES', 120), ('MAX_AUDIO_BYTES', 100), ('MAX_REQUESTS', 0)):
                with self.subTest(limit=name), patch.object(cache, name, value):
                    with self.assertRaises(cache.RecoveryCacheUnavailable):
                        self.save(storage, self.body())
                    self.assertEqual(before, self.index().read_bytes())

    def test_metadata_symlink_hardlink_fifo_never_writes_outside(self):
        with self.open_cache():
            pass
        outside = self.root / 'outside'
        outside.write_bytes(b'keep')
        outside.chmod(0o600)
        for name in (cache.INDEX_NAME, cache.PENDING_NAME, cache.INDEX_NAME + '.lock'):
            path = self.index().with_name(name)
            original = path.read_bytes() if path.exists() else None
            for alias in ('symlink', 'hardlink', 'fifo'):
                with self.subTest(name=name, alias=alias):
                    path.unlink(missing_ok=True)
                    if alias == 'symlink':
                        path.symlink_to(outside)
                    elif alias == 'hardlink':
                        os.link(outside, path)
                    else:
                        os.mkfifo(path, 0o600)
                    with self.assertRaises((OSError, cache.RecoveryCacheUnavailable)):
                        with self.open_cache() as storage:
                            self.save(storage, self.body())
                    self.assertEqual(b'keep', outside.read_bytes())
                    path.unlink()
                    if original is not None:
                        path.write_bytes(original)
                        path.chmod(0o600)

    def test_directory_alias_and_live_namespace_replacement_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        namespace = self.root / cache.CACHE_DIRECTORY
        namespace.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            with self.open_cache():
                pass
        self.assertEqual([], list(outside.iterdir()))
        namespace.unlink()
        with self.open_cache() as storage:
            namespace.rename(self.root / 'old-cache')
            namespace.mkdir(mode=0o700)
            with self.assertRaises(cache.RecoveryCacheUnavailable):
                with storage.read_transaction():
                    pass

    def test_unsafe_existing_permissions_rejected(self):
        with self.open_cache():
            pass
        for path in (self.index(), self.index().parent):
            old_mode = path.stat().st_mode & 0o777
            path.chmod(old_mode | 0o040)
            with self.assertRaises(cache.RecoveryCacheUnavailable):
                with self.open_cache():
                    pass
            path.chmod(old_mode)

    def test_failed_pre_replace_sync_and_replace_keep_old_authority(self):
        with self.open_cache() as storage:
            before = self.index().read_bytes()
            for target in ('synthesis_recovery_cache.os.fsync', 'synthesis_recovery_cache.os.replace'):
                with self.subTest(target=target), patch(target, side_effect=OSError('injected')):
                    with self.assertRaises(OSError):
                        self.save(storage, self.body())
                self.assertEqual(before, self.index().read_bytes())
                with storage.read_transaction() as transaction:
                    self.assertEqual({}, transaction.data['requests'])
            self.save(storage, self.body())

    def test_ambiguous_post_replace_sync_keeps_charge_and_requires_new_transaction(self):
        with self.open_cache() as storage:
            real_sync = os.fsync
            def fail_directory(fd):
                if fd == storage._directory_fd:
                    raise OSError('ambiguous directory durability')
                real_sync(fd)
            with storage.write_transaction() as transaction:
                transaction.data.update(self.body())
                with patch('synthesis_recovery_cache.os.fsync', side_effect=fail_directory):
                    with self.assertRaises(OSError):
                        transaction.commit()
                with self.assertRaises(cache.RecoveryCacheUnavailable):
                    transaction.commit()
            with storage.read_transaction() as transaction:
                self.assertEqual(self.body(), transaction.data)
                self.assertEqual(1024, cache._validate_body(transaction.data))

    def test_actual_process_exit_before_replace_preserves_old_authority(self):
        with self.open_cache() as storage:
            self.save(storage, self.body())
        code = '''import os, sys
from contextlib import nullcontext
import synthesis_recovery_cache as cache
with cache.RecoveryCacheStorage(sys.argv[1], nullcontext) as storage:
 cache.os.replace = lambda *args, **kwargs: os._exit(71)
 with storage.write_transaction() as transaction:
  transaction.data['requests'].clear()
  transaction.commit()
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], timeout=5,
                                capture_output=True, text=True)
        self.assertEqual(71, result.returncode, result.stderr)
        with self.open_cache() as storage:
            with storage.read_transaction() as transaction:
                self.assertEqual(self.body(), transaction.data)


    def audio_source(self):
        source = self.root / 'source.wav'
        frames, rate = 2400, 24000
        samples = struct.pack('<' + 'h' * frames,
                              *(int(8000 * math.sin(i * 0.11)) for i in range(frames)))
        with wave.open(str(source), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(samples)
        metadata = dict(size=source.stat().st_size, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        frames=frames, rate=rate, channels=1)
        os.utime(source, (1, 1))  # A read may legitimately update access time.
        return source, metadata

    def save_audio(self, storage, source, metadata, *, key='a' * 64, index=0, budget=None):
        return storage.save_window(key, 'b' * 64, index, 'd' * 64, source, metadata,
                                   budget_bytes=budget or metadata['size'] * 2)

    def test_real_pcm_restart_capture_is_private_and_retirement_frees_only_known_paths(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertTrue(self.save_audio(storage, source, metadata))
        unknown = self.index().with_name('unrelated-owner-file')
        unknown.write_bytes(b'keep')
        with self.open_cache() as storage:
            destination = self.root / 'working.wav'
            entries = self.entries
            row = storage.capture_window('a' * 64, 'b' * 64, 0, destination)
            self.assertEqual(entries, self.entries)
            self.assertEqual('complete', row['state'])
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            with wave.open(str(destination), 'rb') as audio:
                self.assertEqual(metadata['frames'], audio.getnframes())
                self.assertEqual(metadata['rate'], audio.getframerate())
            storage.retire_request('a' * 64)
            with storage.read_transaction() as transaction:
                self.assertEqual({}, transaction.data['requests'])
            self.assertEqual(b'keep', unknown.read_bytes())
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'absent.wav'))
            self.assertFalse((self.root / 'absent.wav').exists())

    def test_capacity_refuses_without_payload_bytes_or_eviction(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertFalse(self.save_audio(storage, source, metadata, budget=metadata['size'] - 1))
            self.assertEqual([], list(self.index().parent.glob('*.wav')))
            self.assertTrue(self.save_audio(storage, source, metadata, budget=metadata['size']))
            self.assertFalse(self.save_audio(storage, source, metadata, key='f' * 64, budget=metadata['size']))
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
                self.assertEqual(['a' * 64], list(transaction.data['requests']))

    def test_new_claim_adopts_complete_audio_and_reconciles_interrupted_window(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertTrue(self.save_audio(storage, source, metadata))
            with patch.object(storage, '_copy_exact', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    self.save_audio(storage, source, metadata, index=1)
            storage.prepare_request('a' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'] * 3)
            with storage.read_transaction() as transaction:
                rows = transaction.data['requests']['a' * 64]['windows']
                self.assertEqual(['0'], list(rows))
                self.assertEqual('f' * 64, rows['0']['claim'])
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
            destination = self.root / 'adopted.wav'
            storage.capture_window('a' * 64, 'b' * 64, 0, destination)
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            self.assertEqual([], list(self.index().parent.glob('*.stage')))

    def test_reconciliation_sync_failure_keeps_reservation_charged_and_retryable(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            with patch.object(storage, '_copy_exact', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    self.save_audio(storage, source, metadata)
            with patch.object(cache.os, 'fsync', side_effect=OSError('directory sync')):
                with self.assertRaises(OSError):
                    storage.prepare_request('a' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'] * 2)
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
            storage.prepare_request('a' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'] * 2)
            with storage.read_transaction() as transaction:
                self.assertEqual(0, cache._validate_body(transaction.data))

    def test_retention_protects_current_claim_parents_and_preserves_unknown_files(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertTrue(self.save_audio(storage, source, metadata))
            self.assertTrue(self.save_audio(storage, source, metadata, key='e' * 64))
            storage.prepare_request('a' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'] * 3)
            unknown = self.index().with_name('keep.wav')
            unknown.write_bytes(b'unknown')
            storage.prepare_request('c' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'])
            with storage.read_transaction() as transaction:
                self.assertEqual(['a' * 64], list(transaction.data['requests']))
            self.assertEqual(b'unknown', unknown.read_bytes())
            # Capacity does not authorize retirement of this epoch's adopted audio.
            storage.prepare_request('c' * 64, 'b' * 64, 'f' * 64, budget_bytes=metadata['size'])
            self.assertFalse(self.save_audio(storage, source, metadata, key='c' * 64, budget=metadata['size']))

    def test_partial_copy_failure_remains_charged_until_admitted_retirement(self):
        source, metadata = self.audio_source()
        def fail_copy(source_handle, destination, row):
            self.assertEqual(0, self.depth)
            destination.write(source_handle.read(17))
            raise OSError('interrupted payload')
        with self.open_cache() as storage:
            with patch.object(storage, '_copy_exact', side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, 'interrupted payload'):
                    self.save_audio(storage, source, metadata)
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
                self.assertEqual('reserved', transaction.data['requests']['a' * 64]['windows']['0']['state'])
            self.assertEqual([17], [p.stat().st_size for p in self.index().parent.glob('*.stage')])
            self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'no.wav'))
            storage.retire_request('a' * 64)
            self.assertEqual([], list(self.index().parent.glob('*.stage')))
            self.assertTrue(self.save_audio(storage, source, metadata))

    def test_promoted_file_stays_reserved_when_completed_index_commit_fails(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            real_save = storage._save_index
            def fail_complete(body):
                if any(row['state'] == 'complete' for request in body['requests'].values()
                       for row in request['windows'].values()):
                    raise OSError('interrupted completion')
                return real_save(body)
            with patch.object(storage, '_save_index', side_effect=fail_complete):
                with self.assertRaises(OSError):
                    self.save_audio(storage, source, metadata)
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
                self.assertEqual('reserved', transaction.data['requests']['a' * 64]['windows']['0']['state'])
            self.assertEqual(1, len(list(self.index().parent.glob('*.wav'))))
            self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'no.wav'))
            storage.retire_request('a' * 64)
            self.assertEqual([], list(self.index().parent.glob('*.wav')))

    def test_retirement_unlink_and_sync_failures_keep_accounting_until_retry(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertTrue(self.save_audio(storage, source, metadata))
            before = self.index().read_bytes()
            with patch.object(cache.os, 'unlink', side_effect=OSError('busy file')):
                with self.assertRaises(OSError):
                    storage.retire_request('a' * 64)
            self.assertEqual(before, self.index().read_bytes())
            real_sync = os.fsync
            def fail_directory(fd):
                if fd == storage._directory_fd:
                    raise OSError('unsynced deletion')
                real_sync(fd)
            with patch.object(cache.os, 'fsync', side_effect=fail_directory):
                with self.assertRaises(OSError):
                    storage.retire_request('a' * 64)
            self.assertEqual(before, self.index().read_bytes())
            self.assertEqual([], list(self.index().parent.glob('*.wav')))
            storage.retire_request('a' * 64)
            with storage.read_transaction() as transaction:
                self.assertEqual(0, cache._validate_body(transaction.data))

    def test_corrupt_payload_and_aliases_refuse_capture_without_overwriting_private_file(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            self.assertTrue(self.save_audio(storage, source, metadata))
            artifact = next(self.index().parent.glob('*.wav'))
            artifact.write_bytes(b'x' * metadata['size'])
            with self.assertRaisesRegex(cache.RecoveryCacheUnavailable, 'payload_integrity_mismatch'):
                storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'bad.wav')
            artifact.unlink()
            artifact.symlink_to(source)
            with self.assertRaises(OSError):
                storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'alias.wav')
            self.assertFalse((self.root / 'alias.wav').exists())
            artifact.unlink()
            os.link(source, artifact)
            with self.assertRaises(cache.RecoveryCacheUnavailable):
                storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'hardlink.wav')
            self.assertFalse((self.root / 'hardlink.wav').exists())
            artifact.unlink()
            artifact.write_bytes(source.read_bytes())
            artifact.chmod(0o600)
            occupied = self.root / 'occupied.wav'
            occupied.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                storage.capture_window('a' * 64, 'b' * 64, 0, occupied)
            self.assertEqual(b'keep', occupied.read_bytes())

    def test_old_reservation_retired_before_copy_cannot_create_uncharged_bytes(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            original_read = storage.read_transaction
            @contextmanager
            def retired_before_copy():
                storage.retire_request('a' * 64)
                with original_read() as transaction:
                    yield transaction
            with patch.object(storage, 'read_transaction', side_effect=retired_before_copy):
                with self.assertRaisesRegex(cache.RecoveryCacheUnavailable, 'reservation_changed'):
                    self.save_audio(storage, source, metadata)
            self.assertEqual([], list(self.index().parent.glob('*.stage')))
            self.assertEqual([], list(self.index().parent.glob('*.wav')))

    def test_actual_process_exit_after_completed_pcm_allows_new_owner_capture(self):
        source, metadata = self.audio_source()
        code = """import os, sys, json
from contextlib import nullcontext
import synthesis_recovery_cache as cache
with cache.RecoveryCacheStorage(sys.argv[1], nullcontext) as storage:
 storage.save_window('a'*64, 'b'*64, 0, 'd'*64, sys.argv[2], json.loads(sys.argv[3]), budget_bytes=16000)
 os._exit(72)
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.root), str(source), json.dumps(metadata)],
                                timeout=5, capture_output=True, text=True)
        self.assertEqual(72, result.returncode, result.stderr)
        with self.open_cache() as storage:
            destination = self.root / 'restart.wav'
            self.assertIsNotNone(storage.capture_window('a' * 64, 'b' * 64, 0, destination))
            self.assertEqual(source.read_bytes(), destination.read_bytes())


    def test_every_save_sync_boundary_retains_charge_for_all_payload_bytes(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            real_sync = os.fsync
            for failure_at in range(1, 7):
                with self.subTest(sync_phase=failure_at):
                    calls = []
                    def fail_sync(fd):
                        calls.append(fd)
                        if len(calls) == failure_at:
                            raise OSError('injected sync boundary')
                        return real_sync(fd)
                    with patch.object(cache.os, 'fsync', side_effect=fail_sync):
                        with self.assertRaisesRegex(OSError, 'injected sync boundary'):
                            self.save_audio(storage, source, metadata)
                    with storage.read_transaction() as transaction:
                        charge = cache._validate_body(transaction.data)
                    payloads = [*self.index().parent.glob('*.wav'), *self.index().parent.glob('*.stage')]
                    self.assertLessEqual(sum(path.stat().st_size for path in payloads), charge)
                    storage.retire_request('a' * 64)
                    with storage.read_transaction() as transaction:
                        self.assertEqual(0, cache._validate_body(transaction.data))
                    self.assertEqual([], list(self.index().parent.glob('*.wav')))
                    self.assertEqual([], list(self.index().parent.glob('*.stage')))

    def test_cancel_at_post_copy_admission_propagates_with_charged_unusable_stage(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            original = storage.admit
            calls = []
            @contextmanager
            def cancel_after_reserve():
                calls.append(1)
                if len(calls) == 2:
                    raise LookupError('cancelled owner after copying')
                with original():
                    yield
            with patch.object(storage, 'admit', cancel_after_reserve):
                with self.assertRaisesRegex(LookupError, 'cancelled owner after copying'):
                    self.save_audio(storage, source, metadata)
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
            self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'no.wav'))
            self.assertEqual(1, len(list(self.index().parent.glob('*.stage'))))
            storage.retire_request('a' * 64)

    def test_source_mutation_after_copy_refuses_completion(self):
        source, metadata = self.audio_source()
        with self.open_cache() as storage:
            real_copy = storage._copy_exact
            def mutate_source(*args):
                real_copy(*args)
                with source.open('ab') as handle:
                    handle.write(b'changed')
            with patch.object(storage, '_copy_exact', side_effect=mutate_source):
                with self.assertRaisesRegex(cache.RecoveryCacheUnavailable, 'source_changed'):
                    self.save_audio(storage, source, metadata)
            with storage.read_transaction() as transaction:
                self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
            self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'no.wav'))
            storage.retire_request('a' * 64)

    def test_process_exit_after_stage_or_payload_rename_keeps_reserved_not_reusable(self):
        source, metadata = self.audio_source()
        code = """import os, sys, json
from contextlib import contextmanager
import synthesis_recovery_cache as cache
entries = 0
@contextmanager
def owner():
 global entries
 entries += 1
 if sys.argv[4] == 'stage' and entries == 3:
  os._exit(73)
 yield
with cache.RecoveryCacheStorage(sys.argv[1], owner) as storage:
 if sys.argv[4] == 'rename':
  original = cache.os.rename
  def interrupted(*args, **kwargs):
   original(*args, **kwargs)
   os._exit(74)
  cache.os.rename = interrupted
 storage.save_window('a'*64, 'b'*64, 0, 'd'*64, sys.argv[2], json.loads(sys.argv[3]), budget_bytes=16000)
"""
        for boundary, returncode in (('stage', 73), ('rename', 74)):
            with self.subTest(boundary=boundary):
                result = subprocess.run([sys.executable, '-c', code, str(self.root), str(source),
                                         json.dumps(metadata), boundary], timeout=5, capture_output=True, text=True)
                self.assertEqual(returncode, result.returncode, result.stderr)
                with self.open_cache() as storage:
                    with storage.read_transaction() as transaction:
                        self.assertEqual(metadata['size'], cache._validate_body(transaction.data))
                        self.assertEqual('reserved', transaction.data['requests']['a' * 64]['windows']['0']['state'])
                    self.assertIsNone(storage.capture_window('a' * 64, 'b' * 64, 0, self.root / 'no.wav'))
                    storage.retire_request('a' * 64)


class RecoveryPlatformTests(unittest.TestCase):
    def test_unsupported_platform_and_unowned_entry_have_no_cache_io(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform), patch.object(cache.sys, 'platform', platform):
                with patch.object(cache.os, 'open', side_effect=AssertionError('no cache IO')):
                    with self.assertRaisesRegex(cache.RecoveryCacheUnavailable, 'unsupported_durability'):
                        with cache.RecoveryCacheStorage('/missing', None):
                            pass
        with patch.object(cache, 'is_recovery_storage_platform_supported', return_value=True):
            with patch.object(cache.os, 'open', side_effect=AssertionError('no cache IO')):
                with self.assertRaisesRegex(cache.RecoveryCacheUnavailable, 'unowned_request'):
                    with cache.RecoveryCacheStorage('/missing', None):
                        pass
