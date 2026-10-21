"""Real immutable bytes, bounded reservations and recoverable history actions."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from audio_take_history import AudioTakeHistory


class AudioTakeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'selected.wav'
        sf.write(self.source, .2 * np.sin(2 * np.pi * 220 * np.arange(2400) / 24000), 24000)
        self.history = AudioTakeHistory(self.root); self.before = self.source.read_bytes()
        self.book, self.uid = 'a' * 64, 'stable-uid'
        self.captured = dict(text='A synthetic sentence.', speaker='NARRATOR', take=3, audio_revision='old-revision')

    def capture(self):
        return self.history.capture(self.source, self.book, self.uid, self.captured)

    def change(self, row, action, referenced=False):
        return self.history.change(row['version'], row['sha256'], action, lambda _: referenced)

    def test_exact_immutable_deduplicated_descriptor_and_retained_metadata(self):
        row = self.capture()
        self.assertEqual(row['sha256'], hashlib.sha256(self.before).hexdigest())
        self.assertEqual(row['shape'], dict(frames=2400, sample_rate=24000, channels=1))
        self.assertEqual(row, self.capture()); self.assertEqual(self.history.list()['total'], 1)
        self.source.write_bytes(b'new current bytes')
        stream, size, suffix = self.history.open(row['version'], row['sha256'])
        with stream: self.assertEqual(stream.read(), self.before)
        self.assertEqual((size, suffix), (len(self.before), '.wav'))
        record = json.loads(self.history.manifest.read_text())['records'][0]
        self.assertEqual(record['captured'], self.captured); self.assertNotIn('selected', record)

    def test_delete_undo_protection_reference_fence_and_explicit_purge(self):
        row = self.capture(); self.change(row, 'protect')
        for action in ('delete', 'purge'):
            with self.assertRaisesRegex(ValueError, 'protected'): self.change(row, action)
        self.change(row, 'unprotect')
        with self.assertRaisesRegex(ValueError, 'referenced'): self.change(row, 'delete', True)
        self.change(row, 'delete'); self.assertEqual(self.history.list()['usage']['bytes'], len(self.before))
        with self.assertRaises(ValueError): self.history.open(row['version'], row['sha256'])
        self.change(row, 'undo'); self.assertEqual(self.history.list()['records'][0]['state'], 'ready')
        self.change(row, 'delete')
        with self.assertRaisesRegex(ValueError, 'referenced'): self.change(row, 'purge', True)
        self.change(row, 'purge'); self.assertEqual(self.history.list()['usage']['bytes'], 0)
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_capacity_includes_deleted_and_pending_and_metadata_is_bounded(self):
        for name, value in (('MAX_LINE_RECORDS', 1), ('MAX_RECORDS', 1), ('MAX_PAYLOAD_BYTES', len(self.before))):
            with self.subTest(limit=name), patch('audio_take_history.' + name, value):
                row = self.capture(); self.change(row, 'delete')
                with self.assertRaisesRegex(ValueError, 'full'):
                    self.history.capture(self.source, self.book, self.uid, dict(self.captured, take=4))
                self.assertEqual(self.history.list()['total'], 1); self.change(row, 'purge')
        with patch('audio_take_history.MAX_MANIFEST_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'metadata is full'): self.capture()
        self.assertEqual(list(self.history.directory.glob('*.wav')), [])

    def test_failed_decode_retains_charged_pending_copy(self):
        self.source.write_bytes(b'invalid encoded audio')
        with self.assertRaises((RuntimeError, ValueError)): self.capture()
        result = self.history.list(); row = result['records'][0]
        self.assertEqual(row['state'], 'pending'); self.assertEqual(result['usage']['bytes'], len(b'invalid encoded audio'))
        self.change(row, 'purge'); self.assertEqual(self.history.list()['total'], 0)

    def test_ready_metadata_failure_preserves_selected_bytes_and_pending_charge(self):
        save = self.history._save
        def fail_ready(document):
            if document['records'][0]['state'] == 'ready': raise OSError('ready metadata failed')
            return save(document)
        with patch.object(self.history, '_save', side_effect=fail_ready):
            with self.assertRaisesRegex(OSError, 'ready metadata failed'): self.capture()
        row = AudioTakeHistory(self.root).list()['records'][0]
        self.assertEqual(row['state'], 'pending'); self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual((self.history.directory / (row['version'] + '.wav')).read_bytes(), self.before)
        self.change(row, 'purge')

    def test_process_death_reservation_copy_and_purge_are_recoverable(self):
        code = '''import os,sys
from pathlib import Path
from audio_take_history import AudioTakeHistory
h=AudioTakeHistory(sys.argv[1]);stage=sys.argv[2];save=h._save
def stop(d):
 save(d)
 state=d['records'][0]['state'] if d['records'] else 'empty'
 if stage=='reserve' and state=='pending': os._exit(91)
 if stage=='copy' and state=='ready': os._exit(92)
 if stage=='purge' and state=='purging':
  h._path(d['records'][0]).unlink();os._exit(93)
h._save=stop
if stage=='purge':
 r=h.list()['records'][0];h.change(r['version'],r['sha256'],'purge',lambda _:False)
else:h.capture(Path(sys.argv[1])/'selected.wav','a'*64,'stable-uid',{'take':3})
'''
        for stage, status in (('reserve', 91), ('copy', 92), ('purge', 93)):
            with self.subTest(stage=stage):
                if stage == 'purge': row = self.capture(); self.change(row, 'delete')
                result = subprocess.run([sys.executable, '-c', code, str(self.root), stage], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, status, result.stderr)
                rows = self.history.list()['records']; self.assertEqual(len(rows), 1)
                self.assertEqual(self.history.list()['usage']['bytes'], len(self.before)); row = rows[0]
                if row['state'] == 'ready': self.change(row, 'delete')
                self.change(row, 'purge'); self.assertEqual(self.source.read_bytes(), self.before)

    def test_corruption_wrong_identity_aliases_and_unknown_files_refuse(self):
        row = self.capture()
        with self.assertRaises(ValueError): self.history.open('b' * 32, row['sha256'])
        with self.assertRaises(ValueError): self.history.open(row['version'], 'b' * 64)
        payload = self.history.directory / (row['version'] + '.wav'); payload.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'bytes changed'): self.history.open(row['version'], row['sha256'])
        with self.assertRaisesRegex(ValueError, 'bytes changed'): self.capture()
        payload.unlink(); payload.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'aliases'): self.history.open(row['version'], row['sha256'])
        payload.unlink(); payload.write_bytes(self.before)
        unknown = self.history.directory / 'foreign.wav'; unknown.write_bytes(self.before)
        with self.assertRaisesRegex(ValueError, 'Unknown'): self.change(row, 'delete')
        self.assertEqual(unknown.read_bytes(), self.before)

    def test_source_alias_hardlink_nonfinite_and_oversize_refuse(self):
        alias = self.root / 'alias.wav'; alias.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'aliases'): self.history.capture(alias, self.book, self.uid, self.captured)
        alias.unlink(); os.link(self.source, alias)
        with self.assertRaisesRegex(ValueError, 'hard links'): self.capture()
        alias.unlink()
        with patch('audio_take_history.MAX_AUDIO_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'bounded'): self.capture()
        with self.assertRaisesRegex(ValueError, 'too much'):
            self.history.capture(self.source, self.book, self.uid, {'text': 'a' * 131072})
        sf.write(self.source, np.array([0., np.nan, 0.]), 24000, subtype='FLOAT')
        with self.assertRaisesRegex(ValueError, 'non-finite'): self.capture()
        self.assertEqual(self.history.list()['records'][0]['state'], 'pending')

    def test_concurrent_capture_deduplicates_and_book_generations_stay_distinct(self):
        with ThreadPoolExecutor(max_workers=4) as pool: rows = list(pool.map(lambda _: self.capture(), range(4)))
        self.assertEqual(len({r['version'] for r in rows}), 1)
        other = self.history.capture(self.source, 'b' * 64, self.uid, self.captured)
        self.assertNotEqual(other['version'], rows[0]['version'])
        self.assertEqual(self.history.list(self.book, self.uid)['total'], 1); self.assertEqual(self.history.list()['total'], 2)

    def test_empty_pending_and_failed_cleanup_remain_charged_until_retry(self):
        row = self.capture(); self.change(row, 'delete'); save = self.history._save
        def fail_finish(document):
            if not document['records']: raise OSError('cleanup metadata failed')
            save(document)
        with patch.object(self.history, '_save', side_effect=fail_finish):
            with self.assertRaisesRegex(OSError, 'cleanup metadata failed'): self.change(row, 'purge')
        self.assertEqual(self.history.list()['records'][0]['state'], 'purging')
        self.assertEqual(self.history.list()['usage']['bytes'], len(self.before)); self.change(row, 'purge')
        with patch('audio_take_history._decode', side_effect=ValueError('bad copy')):
            with self.assertRaises(ValueError): self.capture()
        pending = self.history.list()['records'][0]
        (self.history.directory / (pending['version'] + '.wav')).write_bytes(b'')
        self.change(pending, 'purge'); self.assertEqual(self.history.list()['total'], 0)

    def test_exact_persisted_metadata_bytes_are_bounded_and_pending_write_retries(self):
        row = self.capture()
        stored = self.history.manifest.read_bytes()
        self.assertEqual(len(stored), len(json.dumps(json.loads(stored), ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')))
        # Exact-capacity writes remain readable; a pending file never becomes authority.
        with patch('audio_take_history.MAX_MANIFEST_BYTES', len(stored)):
            self.assertEqual(self.history.list()['total'], 1)
        code = """import os,sys
from audio_take_history import AudioTakeHistory
h=AudioTakeHistory(sys.argv[1]);r=h.list()['records'][0]
os.replace=lambda *a,**k: os._exit(97)
h.change(r['version'],r['sha256'],'delete',lambda _:False)
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertTrue(self.history.pending.exists())
        self.assertEqual(self.history.list()['records'][0]['state'], 'ready')
        self.change(row, 'delete'); self.change(row, 'purge')
        self.assertEqual(self.history.list()['total'], 0)
        self.assertFalse(self.history.pending.exists())

    def test_truncated_wav_header_is_not_reinterpreted_as_a_valid_short_take(self):
        import struct
        self.assertEqual(self.before[36:40], b'data')
        oversized_data = bytearray(self.before)
        struct.pack_into('<I', oversized_data, 40, 9600)
        short_riff = bytearray(self.before)
        struct.pack_into('<I', short_riff, 4, 28)
        for malformed in (self.before[:-200], oversized_data, short_riff):
            with self.subTest(kind=bytes(malformed[:44])):
                self.source.write_bytes(malformed)
                with self.assertRaisesRegex(ValueError, 'truncated|RIFF extent'):
                    self.capture()
                listing = self.history.list()
                self.assertEqual(listing['records'][0]['state'], 'pending')
                self.assertEqual(self.source.read_bytes(), malformed)
                self.assertEqual(listing['usage']['bytes'], sum(r['bytes'] for r in listing['records']))

    def test_full_unicode_record_is_bounded_before_reservation(self):
        captured = {'text': 'x' * (128 * 1024 - 2048 - 11)}
        with self.assertRaisesRegex(ValueError, 'record is invalid'):
            self.history.capture(self.source, self.book, self.uid, captured, book_label='😀' * 256)
        self.assertEqual(self.history.list()['total'], 0)
        self.assertEqual(list(self.history.directory.glob('*.wav')), [])

    def test_fd_wrapper_failure_closes_raw_file_and_pending_metadata_descriptors(self):
        from audio_take_history import _open_regular
        opened = []
        original = os.open
        def track(*args, **kwargs):
            fd = original(*args, **kwargs); opened.append(fd); return fd
        with patch('audio_take_history.os.open', side_effect=track), patch('audio_take_history.os.fdopen', side_effect=MemoryError('wrapper')):
            with self.assertRaises(MemoryError): _open_regular(self.source, self.root, 65536)
        with self.assertRaises(OSError): os.fstat(opened[-1])
        self.history.directory.mkdir(exist_ok=True)
        with patch('audio_take_history.os.open', side_effect=track), patch('audio_take_history.os.fdopen', side_effect=MemoryError('wrapper')):
            with self.assertRaises(MemoryError): self.history._save({'version':1, 'records':[]})
        with self.assertRaises(OSError): os.fstat(opened[-1])

    def test_small_manifest_read_does_not_allocate_the_full_archive_limit(self):
        import tracemalloc
        self.capture()
        self.assertLess(self.history.manifest.stat().st_size, 4096)
        tracemalloc.start()
        try:
            self.assertEqual(self.history.list()['total'], 1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * 1024**2)

    def test_malformed_identity_types_and_huge_timestamps_refuse_without_repair(self):
        self.capture()
        original = self.history.manifest.read_bytes()
        for field, value in (('version', int('1'*32)), ('sha256', int('1'*64)),
                             ('book_token', int('1'*64)), ('created', 10**1000)):
            with self.subTest(field=field):
                document = json.loads(original); document['records'][0][field] = value
                encoded = json.dumps(document).encode(); self.history.manifest.write_bytes(encoded)
                with self.assertRaises(ValueError): self.history.list()
                self.assertEqual(self.history.manifest.read_bytes(), encoded)
        self.history.manifest.write_bytes(original)

    def test_lossless_non_wav_capture_audition_and_undo_preserve_exact_payload(self):
        self.source = self.root / 'selected.flac'
        sf.write(self.source, .2 * np.sin(2 * np.pi * 220 * np.arange(2400) / 24000), 24000)
        original = self.source.read_bytes()
        self.assertTrue(original.startswith(b'fLaC'))
        row = self.capture()
        self.assertEqual(row['shape'], dict(frames=2400, sample_rate=24000, channels=1))
        self.change(row, 'delete'); self.change(row, 'undo')
        stream, size, suffix = self.history.open(row['version'], row['sha256'])
        with stream: self.assertEqual(stream.read(), original)
        self.assertEqual((size, suffix), (len(original), '.flac'))
        self.assertEqual(self.source.read_bytes(), original)

    def test_source_and_decoded_copy_mutation_refuse_without_selecting_partial_audio(self):
        from audio_take_history import _hash, _decode
        def change_source(stream, limit, **kwargs):
            result = _hash(stream, limit, **kwargs)
            self.source.write_bytes(self.before + b'changed')
            return result
        with patch('audio_take_history._hash', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, 'changed while hashing'): self.capture()
        self.assertEqual(self.history.list()['total'], 0)
        self.source.write_bytes(self.before)
        def change_copy(stream, **kwargs):
            result = _decode(stream, **kwargs)
            payload = next(self.history.directory.glob('*.wav'))
            with payload.open('ab') as output: output.write(b'changed')
            return result
        with patch('audio_take_history._decode', side_effect=change_copy):
            with self.assertRaisesRegex(ValueError, 'changed during decoding'): self.capture()
        self.assertEqual(self.history.list()['records'][0]['state'], 'pending')
        self.assertEqual(self.source.read_bytes(), self.before)
