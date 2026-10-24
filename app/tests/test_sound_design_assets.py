import tempfile
import hashlib
import unittest
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from unittest.mock import patch

from sound_design_assets import (SoundDesignAssetStore,
    get_sound_asset_manifest_bytes, get_sound_asset_storage_bytes)


class SoundDesignAssetStoreTests(unittest.TestCase):
    def source(self, directory):
        import numpy as np
        import soundfile as sf
        path = Path(directory) / 'input.wav'
        sf.write(path, np.array([-.5, 0, .5] * 1000), 24000, subtype='PCM_16')
        return path

    def store(self, directory, **options):
        return SoundDesignAssetStore(directory, source_limit=65536, pcm_limit=65536,
                                      total_limit=options.get('total_limit', 256000))

    def incomplete(self):
        return dict(asset_id='a' * 32, state='incomplete',
                    source=dict(sha256='b' * 64, bytes=64), reserved_pcm_bytes=1024, canonical=None)

    def test_saved_incomplete_reservation_survives_store_reopen_and_counts_full_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            store = SoundDesignAssetStore(directory)
            with store._locked() as document:
                document['assets'].append(self.incomplete())
                store._save(document)
            other = SoundDesignAssetStore(directory)
            self.assertEqual(1088, get_sound_asset_storage_bytes(other._load()))
            self.assertEqual([self.incomplete()], other._load()['assets'])

    def test_malformed_or_duplicate_metadata_never_overwrites_recoverable_bytes(self):
        for raw in (b'{"schema":1,"schema":1,"assets":[]}',
                    b'{"schema":1,"assets":[NaN]}', b'{"schema":true,"assets":[]}',
                    b'{"schema":1,"assets":[],"unknown":1}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                directory = str(Path(directory).resolve())
                store = SoundDesignAssetStore(directory)
                store.directory.mkdir()
                store.manifest.write_bytes(raw)
                with self.assertRaises(ValueError), store._locked():
                    self.fail('Invalid metadata admitted')
                self.assertEqual(raw, store.manifest.read_bytes())

    def test_incomplete_cannot_claim_ready_audio_and_unknown_artifacts_remain_for_recovery(self):
        row = self.incomplete()
        row['canonical'] = {'bytes': 4}
        with self.assertRaises(ValueError):
            get_sound_asset_manifest_bytes({'schema': 1, 'assets': [row]})
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            store = SoundDesignAssetStore(directory)
            store.directory.mkdir()
            orphan = store.directory / '.publication-interrupted'
            orphan.write_bytes(b'Recoverable interrupted metadata')
            with self.assertRaisesRegex(ValueError, 'recovery'), store._locked():
                self.fail('Unregistered artifact ignored')
            self.assertEqual(b'Recoverable interrupted metadata', orphan.read_bytes())

    def test_import_reopen_dedupe_and_verified_read_preserve_original_and_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            source = self.source(directory)
            original = source.read_bytes()
            store = self.store(directory)
            descriptor = store.import_asset('input.wav')
            self.assertEqual(original, source.read_bytes())
            other = self.store(directory)
            self.assertEqual(descriptor, other.import_asset('input.wav'))
            self.assertEqual(1, len(other.list_assets()['assets']))
            self.assertEqual(len(original) + 12000, other.list_assets()['charged_bytes'])
            with other.open_canonical(descriptor['asset_id']) as (stream, selected):
                self.assertEqual(descriptor, selected)
                self.assertEqual((b'\0\0\0\xc0' + b'\0' * 4 + b'\0\0\0@') * 1000, stream.read())
            with other.open_verified_asset(descriptor['asset_id']) as (stream, selected, evidence):
                pcm = stream.read()
                self.assertEqual(descriptor, selected)
                self.assertEqual({'sha256': hashlib.sha256(original).hexdigest(), 'bytes': len(original)},
                                 evidence['source'])
                self.assertEqual(other.list_assets()['assets'][0]['canonical'], evidence['canonical'])
                self.assertEqual(hashlib.sha256(pcm).hexdigest(), evidence['canonical']['sha256'])
                self.assertEqual(len(pcm), evidence['canonical']['bytes'])
                evidence['source']['sha256'] = '0' * 64
                evidence['canonical']['bytes'] = 0
            self.assertEqual(hashlib.sha256(original).hexdigest(),
                             other.list_assets()['assets'][0]['source']['sha256'])
            self.assertEqual(12000, other.list_assets()['assets'][0]['canonical']['bytes'])
            with self.assertRaisesRegex(ValueError, 'retained'):
                other.purge_incomplete(descriptor['asset_id'])

    def test_damaged_canonical_refuses_read_and_dedupe_without_erasing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            self.source(directory)
            store = self.store(directory)
            descriptor = store.import_asset('input.wav')
            path = store.directory / (descriptor['asset_id'] + '.pcm')
            damaged = b'x' + path.read_bytes()[1:]
            path.write_bytes(damaged)
            for reader in (store.open_canonical, store.open_verified_asset):
                with self.subTest(reader=reader.__name__), self.assertRaisesRegex(ValueError, 'bytes changed'), reader(descriptor['asset_id']):
                    self.fail('Damaged audio admitted')
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                store.import_asset('input.wav')
            self.assertEqual(damaged, path.read_bytes())

    def test_failed_decode_retains_charged_copy_until_explicit_recovery(self):
        import sound_design_decode
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            source = self.source(directory)
            store = self.store(directory)
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=ValueError('Decoder fixture')):
                with self.assertRaisesRegex(ValueError, 'Decoder fixture'):
                    store.import_asset('input.wav')
            listed = store.list_assets()
            self.assertEqual(source.stat().st_size + 65536, listed['charged_bytes'])
            row = listed['assets'][0]
            self.assertEqual('incomplete', row['state'])
            self.assertEqual(source.read_bytes(), (store.directory / (row['asset_id'] + '.source')).read_bytes())
            store.purge_incomplete(row['asset_id'])
            self.assertEqual([], store.list_assets()['assets'])
            self.assertEqual(0, store.list_assets()['charged_bytes'])
            self.assertTrue(source.exists())

    def test_quota_refuses_before_copy_or_decode(self):
        import sound_design_decode
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            self.source(directory)
            store = self.store(directory, total_limit=65536)
            with patch.object(sound_design_decode, 'decode_sound_design_asset') as decode:
                with self.assertRaisesRegex(ValueError, 'reserve'):
                    store.import_asset('input.wav')
                decode.assert_not_called()
            self.assertEqual([], store.list_assets()['assets'])
            self.assertFalse(list(store.directory.glob('*.source')))

    def test_display_name_survives_reopen_without_changing_identity_on_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            self.source(directory)
            store = self.store(directory)
            name = 'Rain & thunder <night>.wav'
            original = store.import_asset('input.wav', display_name=name)
            reopened = self.store(directory)
            self.assertEqual(name, reopened.list_assets()['assets'][0]['display_name'])
            duplicate = reopened.import_asset('input.wav', display_name='Different filename.wav')
            self.assertEqual(original, duplicate)
            self.assertEqual(name, reopened.list_assets()['assets'][0]['display_name'])

    def test_invalid_display_names_refuse_before_creating_store_or_decoding(self):
        for value in ('', ' ', 'x'*129, 'line\nbreak', 'nul\0name', '\ud800', 123):
            with self.subTest(value=repr(value)), tempfile.TemporaryDirectory() as directory:
                directory = str(Path(directory).resolve())
                self.source(directory)
                store = self.store(directory)
                with self.assertRaises(ValueError):
                    store.import_asset('input.wav', display_name=value)
                self.assertFalse(store.directory.exists())

    def test_lowered_quota_allows_inspection_and_recovery_but_no_new_import(self):
        import sound_design_decode
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            source = self.source(directory)
            store = self.store(directory)
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=ValueError('Synthetic decode failure')):
                with self.assertRaises(ValueError):
                    store.import_asset('input.wav')
            reduced = self.store(directory, total_limit=1024)
            listed = reduced.list_assets()
            self.assertGreater(listed['charged_bytes'], listed['limit_bytes'])
            with patch.object(sound_design_decode, 'decode_sound_design_asset') as decode:
                with self.assertRaisesRegex(ValueError, 'reserve'):
                    reduced.import_asset('input.wav')
                decode.assert_not_called()
            reduced.purge_incomplete(listed['assets'][0]['asset_id'])
            self.assertEqual(0, reduced.list_assets()['charged_bytes'])
            self.assertTrue(source.exists())

    def test_recovery_waits_for_live_import_and_cannot_purge_the_ready_result(self):
        import sound_design_decode
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as workers:
            directory = str(Path(directory).resolve())
            self.source(directory)
            store = self.store(directory)
            entered, release, recovery_started = threading.Event(), threading.Event(), threading.Event()
            decode = sound_design_decode.decode_sound_design_asset
            def pause(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release decoder')
                return decode(*args, **kwargs)
            def recover(asset_id):
                recovery_started.set()
                self.store(directory).purge_incomplete(asset_id)
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=pause):
                imported = workers.submit(store.import_asset, 'input.wav')
                try:
                    self.assertTrue(entered.wait(5))
                    row = store.list_assets()['assets'][0]
                    purged = workers.submit(recover, row['asset_id'])
                    self.assertTrue(recovery_started.wait(2))
                    with self.assertRaises(FutureTimeoutError):
                        purged.result(timeout=.1)
                    self.assertTrue((store.directory / (row['asset_id'] + '.source')).exists())
                finally:
                    release.set()
                descriptor = imported.result(timeout=10)
                with self.assertRaisesRegex(ValueError, 'retained'):
                    purged.result(timeout=5)
            with store.open_canonical(descriptor['asset_id']) as (stream, _):
                self.assertEqual(12000, len(stream.read()))

    def test_cancelled_decode_retains_charged_source_until_explicit_purge(self):
        import sound_design_decode
        from tts import ExportCancelled
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            source = self.source(directory)
            store = self.store(directory)
            cancelled = threading.Event()
            decode = sound_design_decode.decode_sound_design_asset
            def cancel(*args, **kwargs):
                cancelled.set()
                return decode(*args, **kwargs)
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=cancel):
                with self.assertRaises(ExportCancelled):
                    store.import_asset('input.wav', cancel_check=cancelled.is_set)
            listed = store.list_assets()
            self.assertEqual(source.stat().st_size + 65536, listed['charged_bytes'])
            row = listed['assets'][0]
            self.assertEqual('incomplete', row['state'])
            store.purge_incomplete(row['asset_id'])
            self.assertEqual(0, store.list_assets()['charged_bytes'])

    def test_late_ready_publication_error_retains_verifiable_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            self.source(directory)
            store = self.store(directory)
            save = store._save
            def late_error(document):
                save(document)
                if document['assets'][0]['state'] == 'ready':
                    raise OSError('Synthetic late directory sync error')
            with patch.object(store, '_save', side_effect=late_error):
                with self.assertRaisesRegex(OSError, 'late directory sync'):
                    store.import_asset('input.wav')
            row = store.list_assets()['assets'][0]
            self.assertEqual('ready', row['state'])
            with store.open_canonical(row['asset_id']) as (stream, _):
                self.assertEqual(12000, len(stream.read()))

    def test_interrupted_purge_keeps_reservation_until_retry_finishes(self):
        import sound_design_decode
        for boundary in ('second_unlink', 'metadata_save'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                directory = str(Path(directory).resolve())
                source = self.source(directory)
                original = source.read_bytes()
                store = self.store(directory)
                decode = sound_design_decode.decode_sound_design_asset
                def fail_after_decode(*args, **kwargs):
                    decode(*args, **kwargs)
                    raise OSError('Synthetic failure before ready publication')
                with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=fail_after_decode):
                    with self.assertRaises(OSError):
                        store.import_asset('input.wav')
                row = store.list_assets()['assets'][0]
                asset_id = row['asset_id']
                unlink = Path.unlink
                def fail_second(path, *args, **kwargs):
                    if path.name == asset_id + '.pcm':
                        raise OSError('Synthetic unlink failure')
                    return unlink(path, *args, **kwargs)
                fault = (patch.object(Path, 'unlink', fail_second) if boundary == 'second_unlink'
                         else patch.object(store, '_save', side_effect=OSError('Synthetic metadata save failure')))
                with fault, self.assertRaises(OSError):
                    store.purge_incomplete(asset_id)
                reopened = self.store(directory)
                self.assertEqual(len(original) + 65536, reopened.list_assets()['charged_bytes'])
                self.assertFalse((store.directory / (asset_id + '.source')).exists())
                self.assertEqual(boundary == 'second_unlink',
                    (store.directory / (asset_id + '.pcm')).exists())
                reopened.purge_incomplete(asset_id)
                self.assertEqual(0, reopened.list_assets()['charged_bytes'])
                self.assertEqual(original, source.read_bytes())

    def test_actual_process_death_preserves_reservations_or_complete_ready_assets(self):
        child = '''import os,sys
from sound_design_assets import SoundDesignAssetStore
import sound_design_decode
store=SoundDesignAssetStore(sys.argv[1],source_limit=65536,pcm_limit=65536,total_limit=256000)
boundary=sys.argv[2]
save=store._save
def crash_save(document):
    save(document)
    if document['assets'][0]['state']==boundary:os._exit(73)
store._save=crash_save
decode=sound_design_decode.decode_sound_design_asset
def crash_decode(*args,**kwargs):
    result=decode(*args,**kwargs)
    if boundary=='decoded':os._exit(73)
    return result
sound_design_decode.decode_sound_design_asset=crash_decode
store.import_asset('input.wav')
'''
        for boundary in ('incomplete', 'decoded', 'ready'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                directory = str(Path(directory).resolve())
                source = self.source(directory)
                original = source.read_bytes()
                result = subprocess.run([sys.executable, '-c', child, directory, boundary],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
                self.assertEqual(73, result.returncode, result.stderr[-2000:])
                store = self.store(directory)
                row = store.list_assets()['assets'][0]
                self.assertEqual(original, source.read_bytes())
                if boundary == 'ready':
                    with store.open_canonical(row['asset_id']) as (stream, _):
                        self.assertEqual(12000, len(stream.read()))
                    self.assertEqual(len(original) + 12000, store.list_assets()['charged_bytes'])
                else:
                    self.assertEqual('incomplete', row['state'])
                    self.assertEqual(len(original) + 65536, store.list_assets()['charged_bytes'])
                    with self.assertRaisesRegex(ValueError, 'incomplete'), store.open_canonical(row['asset_id']):
                        self.fail('Incomplete asset admitted')
                    store.purge_incomplete(row['asset_id'])
                    self.assertEqual(0, store.list_assets()['charged_bytes'])

    def test_source_hardlink_is_refused_without_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            source = self.source(directory)
            linked = Path(directory) / 'linked.wav'
            os.link(source, linked)
            store = self.store(directory)
            with self.assertRaises(ValueError):
                store.import_asset('linked.wav')
            self.assertEqual([], store.list_assets()['assets'])
            self.assertEqual(source.read_bytes(), linked.read_bytes())
