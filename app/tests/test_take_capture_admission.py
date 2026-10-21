"""Real capture interruptions preserve source bytes and durable quota charges."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import audio_take_history as takes
from audio_generation_owner import (AudioGenerationCancelled,
    ensure_audio_generation_owner, ensure_audio_generation_active)


class TakeCaptureAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'selected.wav'
        sf.write(self.source, np.full(1600000, .125), 24000)
        self.before = self.source.read_bytes()
        self.history = takes.AudioTakeHistory(self.root)
        self.captured = {'text': 'Original words.', 'take': 2, 'audio_revision': 'old'}

    def capture(self, **kwargs):
        return self.history.capture(self.source, 'a' * 64, 'line', self.captured, **kwargs)

    def assert_pending(self, extent):
        result = self.history.list()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['usage']['bytes'], len(self.before))
        row = result['records'][0]
        self.assertEqual(row['state'], 'pending')
        payload = self.history.directory / (row['version'] + '.wav')
        self.assertEqual(payload.stat().st_size if payload.exists() else 0, extent)
        self.assertEqual(self.source.read_bytes(), self.before)
        self.history.change(row['version'], row['sha256'], 'purge', lambda _: False)
        self.assertEqual(self.history.list()['total'], 0)

    def test_file_version_matches_descriptor_and_deduplicates_exact_bytes(self):
        version = takes._version(self.source.stat())
        row = self.capture(expected_source_version=list(version), cancel_check=lambda: False)
        self.assertEqual(row, self.capture(expected_source_version=version, cancel_check=lambda: False))
        self.assertEqual(row['sha256'], hashlib.sha256(self.before).hexdigest())
        self.assertEqual(self.history.list()['total'], 1)
        stream, _, _ = self.history.open(row['version'], row['sha256'])
        with stream: self.assertEqual(stream.read(), self.before)

    def test_replaced_or_rewritten_predecessor_refuses_before_reservation(self):
        for mode in ('replacement', 'same-size-rewrite'):
            with self.subTest(mode=mode):
                version = takes._version(self.source.stat())
                if mode == 'replacement':
                    replacement = self.root/'replacement.wav'
                    replacement.write_bytes(self.before); os.replace(replacement, self.source)
                else:
                    time.sleep(.002)
                    self.source.write_bytes(self.before[:-1] + bytes([self.before[-1] ^ 1]))
                    os.utime(self.source, ns=(self.source.stat().st_atime_ns, version[3]))
                current = self.source.read_bytes()
                with self.assertRaisesRegex(ValueError, 'Prior take changed'):
                    self.capture(expected_source_version=version)
                self.assertEqual(self.history.list()['total'], 0)
                self.assertEqual(list(self.history.directory.glob('*.wav')), [])
                self.assertEqual(self.source.read_bytes(), current)
                self.source.write_bytes(self.before)

    def test_invalid_file_version_is_rejected_without_archive_creation(self):
        for value in ('12345', (), (1, 2, 3, 4), (1, 2, 3, 4, True), (1, 2, 3, 4, '5')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'file version'):
                self.capture(expected_source_version=value)
        self.assertFalse(self.history.directory.exists())
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_cancel_before_admission_does_not_create_archive(self):
        with self.assertRaises(AudioGenerationCancelled): self.capture(cancel_check=lambda: True)
        self.assertFalse(self.history.directory.exists())
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_cancellation_during_source_hash_copy_and_copy_verification(self):
        original_hash, original_open = takes._hash, Path.open
        for stage in ('source-hash', 'copy', 'copy-hash'):
            with self.subTest(stage=stage):
                current = {'phase': None, 'source': None, 'stream': None, 'observed': None}
                def hashed(stream, limit, **kwargs):
                    current['phase'] = 'source-hash' if os.fstat(stream.fileno()).st_ino == self.source.stat().st_ino else 'copy-hash'
                    current['stream'] = stream
                    if current['phase'] == 'source-hash': current['source'] = stream
                    return original_hash(stream, limit, **kwargs)
                def opened(path, mode='r', *args, **kwargs):
                    stream = original_open(path, mode, *args, **kwargs)
                    if path.parent == self.history.directory and mode == 'xb':
                        current.update(phase='copy', stream=current['source'])
                    return stream
                def cancelled():
                    if current['phase'] == stage and current['stream'].tell() >= 1024**2:
                        current['observed'] = current['stream'].tell()
                        return True
                    return False
                with patch.object(takes, '_hash', side_effect=hashed), patch.object(Path, 'open', new=opened):
                    with self.assertRaises(AudioGenerationCancelled): self.capture(cancel_check=cancelled)
                self.assertEqual(current['observed'], 1024**2)
                if stage == 'source-hash':
                    self.assertEqual(self.history.list()['total'], 0)
                    self.assertEqual(self.source.read_bytes(), self.before)
                else:
                    self.assert_pending(1024**2 if stage == 'copy' else len(self.before))

    def test_cancellation_in_riff_validation_and_decoded_blocks_stays_distinct(self):
        original_validator, original_read = takes.validate_wav_container, sf.SoundFile.read
        for stage in ('container', 'decode'):
            with self.subTest(stage=stage):
                current = {'container': None, 'frames': 0, 'cancelled': False}
                def validate(stream, *args, **kwargs):
                    current['container'] = stream
                    try: return original_validator(stream, *args, **kwargs)
                    finally: current['container'] = None
                def read(audio, *args, **kwargs):
                    result = original_read(audio, *args, **kwargs)
                    current['frames'] += len(result)
                    return result
                def cancelled():
                    result = (stage == 'container' and current['container'] is not None
                              and current['container'].tell() >= 20 or
                              stage == 'decode' and current['frames'] >= 65536)
                    current['cancelled'] |= result
                    return result
                with patch.object(takes, 'validate_wav_container', side_effect=validate), patch.object(sf.SoundFile, 'read', new=read):
                    with self.assertRaises(AudioGenerationCancelled): self.capture(cancel_check=cancelled)
                self.assertTrue(current['cancelled'])
                self.assertEqual(current['frames'], 0 if stage == 'container' else 65536)
                self.assert_pending(len(self.before))

    def test_cancel_after_reservation_and_after_decode_never_commits_ready(self):
        original_save, original_decode = self.history._save, takes._decode
        for stage in ('reserved', 'decoded'):
            with self.subTest(stage=stage):
                cancelled = threading.Event()
                def save(document):
                    original_save(document)
                    if stage == 'reserved': cancelled.set()
                def decode(*args, **kwargs):
                    result = original_decode(*args, **kwargs)
                    if stage == 'decoded': cancelled.set()
                    return result
                with patch.object(self.history, '_save', side_effect=save), patch.object(takes, '_decode', side_effect=decode):
                    with self.assertRaises(AudioGenerationCancelled): self.capture(cancel_check=cancelled.is_set)
                self.assert_pending(0 if stage == 'reserved' else len(self.before))

    def test_deduplicated_copy_verification_is_cancellable_without_changing_record(self):
        self.capture(); before = self.history.manifest.read_bytes()
        original_hash = takes._hash
        verifying = {'stream': None}
        def hashed(stream, *args, **kwargs):
            if os.fstat(stream.fileno()).st_ino != self.source.stat().st_ino: verifying['stream'] = stream
            return original_hash(stream, *args, **kwargs)
        def cancelled():
            return verifying['stream'] is not None and verifying['stream'].tell() >= 1024**2
        with patch.object(takes, '_hash', side_effect=hashed):
            with self.assertRaises(AudioGenerationCancelled): self.capture(cancel_check=cancelled)
        self.assertEqual(self.history.manifest.read_bytes(), before)
        self.assertEqual(self.history.list()['records'][0]['state'], 'ready')
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_owner_cancellation_can_acquire_mutex_during_actual_archive_copy(self):
        mutex, entered, release, cancelled = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
        @contextmanager
        def owner(*, allow_cancelled=False):
            with mutex:
                if cancelled.is_set() and not allow_cancelled: raise AudioGenerationCancelled()
                yield
        original_open = Path.open
        def opened(path, mode='r', *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if path.parent == self.history.directory and mode == 'xb':
                entered.set()
                if not release.wait(3):
                    stream.close(); raise TimeoutError('copy not released')
            return stream
        def worker():
            with ensure_audio_generation_owner(owner):
                return self.capture(cancel_check=ensure_audio_generation_active)
        with patch.object(Path, 'open', new=opened), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(3))
                acquired = mutex.acquire(timeout=1)
                self.assertTrue(acquired, 'Archive copying held the owner mutex')
                if acquired:
                    try: cancelled.set()
                    finally: mutex.release()
            finally: release.set()
            with self.assertRaises(AudioGenerationCancelled): future.result(timeout=3)
        self.assert_pending(0)

    def test_waiting_for_other_process_archive_lock_is_cancellable(self):
        self.history.directory.mkdir()
        marker = self.root/'lock-ready'
        code = '''import sys,time
from pathlib import Path
from utils import file_lock
with file_lock(sys.argv[1]):
 Path(sys.argv[2]).write_text('ready')
 time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(self.history.manifest), str(marker)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(marker.exists())
            cancelled, waiting = threading.Event(), threading.Event()
            polls = []
            def check():
                polls.append(1)
                if len(polls) >= 4: waiting.set()  # At least one failed lock attempt.
                return cancelled.is_set()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.capture, cancel_check=check)
                try:
                    self.assertTrue(waiting.wait(2)); cancelled.set()
                    with self.assertRaises(AudioGenerationCancelled): future.result(timeout=2)
                finally: cancelled.set()
            self.assertIsNone(child.poll(), 'The other lock owner should still be alive')
            self.assertFalse(self.history.manifest.exists())
            self.assertEqual(list(self.history.directory.glob('*.wav')), [])
        finally:
            child.terminate()
            try: child.communicate(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.communicate(timeout=3)
        self.assertEqual(self.source.read_bytes(), self.before)
