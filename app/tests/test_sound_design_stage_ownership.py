"""Native contracts for descriptor-owned sound upload staging and import."""
from contextlib import asynccontextmanager
import errno
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import PropertyMock, patch

from fastapi import FastAPI
import httpx
import numpy as np
import soundfile as sf

from audio_generation_owner import AudioGenerationCancelled
from routers import sound_design as routes
from sound_design_assets import (DIRECTORY as ASSET_DIRECTORY, MAX_PCM_BYTES,
                                 SoundDesignAssetStore)
import sound_design_decode
import sound_design_uploads as uploads
from source_upload_stage import (OwnedSourceUploadStage, get_upload_stage_accounting,
                                 get_upload_stage_inventory)
from tts import ExportCancelled
from utils import file_lock

FRAMES = 24001
RATE = 24000


def synthetic_wav():
    source = io.BytesIO()
    samples = np.sin(np.linspace(0, 8 * np.pi, FRAMES)) * .25
    sf.write(source, samples, RATE, format='WAV', subtype='PCM_16')
    return source.getvalue()


class CancellingStream(io.BytesIO):
    def __init__(self, data, cancelled):
        super().__init__(data)
        self.cancelled = cancelled

    def read(self, size=-1):
        self.cancelled.set()
        return super().read(size)


class SoundDesignStagePrimitiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = synthetic_wav()
        self.staging = self.root / uploads.STAGING_DIRECTORY
        self.staging.mkdir()

    def test_stage_creation_errors_preserve_non_support_failures(self):
        for error in (OSError(errno.ENOTSUP, 'synthetic unsupported primitive'),
                      OSError(errno.EIO, 'synthetic stage I/O failure')):
            with self.subTest(errno=error.errno):
                with patch.object(uploads, 'OwnedSourceUploadStage', side_effect=error):
                    if error.errno == errno.ENOTSUP:
                        with self.assertRaisesRegex(ValueError, 'unnamed-file directory') as caught:
                            uploads._open_sound_upload_stage(self.staging, 64)
                        self.assertIs(error, caught.exception.__cause__)
                    else:
                        with self.assertRaises(OSError) as caught:
                            uploads._open_sound_upload_stage(self.staging, 64)
                        self.assertIs(error, caught.exception)

    def test_stage_primitives_are_platform_correct(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        try:
            stage.write(b'original')
            stage.seal()
            if os.name == 'nt':
                entries = list(self.staging.glob('.source-upload-*.part'))
                self.assertEqual(1, len(entries))
                private = entries[0]
                self.assertEqual(8, private.stat().st_size)
                with self.assertRaises(PermissionError):
                    open(private, 'wb')
                with self.assertRaises(PermissionError):
                    os.unlink(private)
                victim = self.root / 'victim.bin'
                victim.write_bytes(b'victim')
                with self.assertRaises(PermissionError):
                    os.replace(victim, private)
                self.assertEqual(b'victim', victim.read_bytes())
                link = self.staging / 'symlink-entry'
                try:
                    os.symlink(victim, link)
                except OSError as error:
                    self.assertEqual(1314, getattr(error, 'winerror', None), error)
                    self.assertFalse(os.path.lexists(link))
                else:
                    try:
                        with self.assertRaises(PermissionError):
                            os.replace(link, private)
                        self.assertEqual(b'victim', victim.read_bytes())
                        self.assertTrue(link.is_symlink())
                    finally:
                        link.unlink()
                alias = self.staging / 'external-link'
                os.link(private, alias)
                with self.assertRaisesRegex(ValueError, 'changed before publication'):
                    stage.seal()
            else:
                self.assertEqual([], list(self.staging.iterdir()))
                self.assertEqual(0, os.fstat(stage._stream.fileno()).st_nlink)
                self.assertIsNone(stage._accounting_name)
        finally:
            stage.close()
        if os.name == 'nt':
            self.assertFalse(private.exists())
            self.assertEqual(b'original', alias.read_bytes())
            self.assertEqual(1, alias.stat().st_nlink)
            os.unlink(alias)
        self.assertEqual([], list(self.staging.iterdir()))

    def test_stage_root_relocation_fails_closed(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        retained = self.root / 'retained'
        try:
            stage.write(b'payload')
            if os.name == 'nt':
                with self.assertRaises(PermissionError):
                    self.staging.rename(retained)
                self.assertIsNone(stage.seal())
            else:
                self.staging.rename(retained)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    stage.seal()
        finally:
            stage.close()
        directory = retained if os.name != 'nt' else self.staging
        self.assertEqual([], list(directory.iterdir()))

    def test_unsupported_unnamed_primitive_refused_without_named_fallback(self):
        stream = io.BytesIO(self.raw)
        with patch.object(uploads, 'OwnedSourceUploadStage',
                          side_effect=OSError(errno.ENOTSUP, 'unnamed staging unavailable')), \
                patch.object(SoundDesignAssetStore, 'import_owned_source') as admit:
            with self.assertRaisesRegex(ValueError, 'unnamed-file directory'):
                uploads.import_sound_asset_upload(self.root, stream)
            admit.assert_not_called()
        self.assertEqual(0, stream.tell())
        self.assertEqual({'quota.lock'}, {path.name for path in self.staging.iterdir()})
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))
        self.assertFalse((self.root / '.sound_design_assets' / 'manifest.json').exists())

    def test_process_death_stage_outcome_by_primitive(self):
        code = ('import os,sys\n'
                'from source_upload_stage import OwnedSourceUploadStage\n'
                'stage=OwnedSourceUploadStage(sys.argv[1],byte_limit=64)\n'
                "stage.write(b'x'*32)\n"
                'stage.seal()\n'
                'os._exit(23)\n')
        result = subprocess.run([sys.executable, '-c', code, str(self.staging)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(23, result.returncode, result.stderr[-2000:])
        entries = sorted(path.name for path in self.staging.iterdir())
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))
        self.assertFalse((self.root / '.sound_design_assets' / 'manifest.json').exists())
        if os.name == 'nt':
            self.assertEqual(1, len(entries))
            self.assertTrue(entries[0].startswith('.source-upload-'))
            self.assertTrue(entries[0].endswith('.part'))
            self.assertEqual(32, (self.staging / entries[0]).stat().st_size)
            stream = io.BytesIO(b'new')
            with patch.object(uploads, 'MAX_STAGING_BYTES', 32):
                with self.assertRaisesRegex(ValueError, 'full'):
                    uploads.import_sound_asset_upload(self.root, stream)
            self.assertEqual(0, stream.tell())
            self.assertEqual(32, (self.staging / entries[0]).stat().st_size)
        else:
            self.assertEqual([], entries)

    def test_live_unnamed_stage_is_charged(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'x' * 32)
        names, live = get_upload_stage_accounting(self.staging)
        self.assertEqual(32, live)
        self.assertEqual(names, {path.name for path in self.staging.glob('.source-upload-*.part')})
        stream = io.BytesIO(b'y' * 33)
        if os.name == 'nt':
            with patch.object(uploads, 'MAX_STAGING_BYTES', 32):
                with self.assertRaisesRegex(ValueError, 'full'):
                    uploads.import_sound_asset_upload(self.root, stream)
            self.assertEqual(0, stream.tell())
        else:
            with patch.object(uploads, 'MAX_STAGING_BYTES', 64):
                with self.assertRaisesRegex(ValueError, 'allowance'):
                    uploads.import_sound_asset_upload(self.root, stream)
            self.assertEqual(33, stream.tell())
        self.assertEqual(b'x' * 32, stage.read_bytes())
        self.assertEqual(names, {path.name for path in self.staging.glob('.source-upload-*.part')})
        self.assertEqual((names, 32), get_upload_stage_accounting(self.staging))
        self.assertEqual({*names, 'quota.lock'}, {path.name for path in self.staging.iterdir()})
        stage.close()
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))

    def test_live_stage_counts_toward_cap_before_reading_another_upload(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        try:
            stage.write(b'preserve')
            inventory = get_upload_stage_inventory(self.staging)
            self.assertEqual(1, inventory['count'])
            self.assertEqual(8, inventory['bytes'])
            stream = io.BytesIO(self.raw)
            with patch.object(uploads, 'MAX_STAGES', 1):
                with self.assertRaisesRegex(ValueError, 'staging is full'):
                    uploads.import_sound_asset_upload(self.root, stream)
            self.assertEqual(0, stream.tell())
            self.assertEqual(b'preserve', stage.read_bytes())
            self.assertEqual(inventory, get_upload_stage_inventory(self.staging))
        finally:
            stage.close()
        self.assertEqual(0, get_upload_stage_inventory(self.staging)['count'])

    def test_cancel_during_write_leaves_no_stage_or_reservation(self):
        cancelled = threading.Event()
        stream = CancellingStream(self.raw, cancelled)
        with self.assertRaises(AudioGenerationCancelled):
            uploads.import_sound_asset_upload(self.root, stream, cancel_check=cancelled.is_set)
        self.assertFalse(stream.closed)
        self.assertEqual(len(self.raw), stream.tell())
        self.assertEqual({'quota.lock'}, {path.name for path in self.staging.iterdir()})
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))
        self.assertEqual([], SoundDesignAssetStore(self.root).list_assets()['assets'])


class SoundDesignStageImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = synthetic_wav()
        self.staging = self.root / uploads.STAGING_DIRECTORY
        self.staging.mkdir()

    def assert_staging_released(self):
        self.assertEqual({'quota.lock'}, {path.name for path in self.staging.iterdir()})
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))

    def test_cancel_during_initial_hash_stops_before_reservation(self):
        cancelled = threading.Event()
        original = OwnedSourceUploadStage.iter_blocks
        observed = []

        def cancel_after_first(stage, block_size=65536):
            for block in original(stage, block_size):
                observed.append(len(block))
                if len(observed) == 2:
                    cancelled.set()
                yield block

        with patch.object(OwnedSourceUploadStage, 'iter_blocks', cancel_after_first), \
                patch.object(OwnedSourceUploadStage, 'sha256', side_effect=AssertionError('uncancellable hash')):
            with self.assertRaises(AudioGenerationCancelled):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(b'x' * (3 * 65536)),
                                                 cancel_check=cancelled.is_set)
        self.assertEqual([65536, 65536], observed)
        self.assertEqual([], SoundDesignAssetStore(self.root).list_assets()['assets'])
        self.assertFalse(list((self.root / ASSET_DIRECTORY).glob('*.source')))
        self.assert_staging_released()

    def test_real_positive_import_preserves_bytes_and_decoder_names(self):
        seen = {}
        decode = sound_design_decode.decode_sound_design_asset

        def record(root, source_name, output_name, **kwargs):
            seen.update(source=source_name, output=output_name)
            return decode(root, source_name, output_name, **kwargs)

        with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=record):
            descriptor = uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        asset_id = descriptor['asset_id']
        self.assertEqual(f'{ASSET_DIRECTORY}/{asset_id}.source', seen['source'])
        self.assertEqual(f'{ASSET_DIRECTORY}/{asset_id}.pcm', seen['output'])
        self.assertNotIn(uploads.STAGING_DIRECTORY, seen['source'])
        self.assertEqual(FRAMES, descriptor['frames'])
        store = SoundDesignAssetStore(self.root)
        row = store.list_assets()['assets'][0]
        self.assertEqual(self.raw, (store.directory / (asset_id + '.source')).read_bytes())
        with store.open_canonical(asset_id) as (stream, selected):
            pcm = stream.read()
        self.assertEqual(descriptor, selected)
        self.assertEqual(FRAMES * 4, len(pcm))
        self.assertEqual(row['canonical']['sha256'], hashlib.sha256(pcm).hexdigest())
        self.assert_staging_released()

    def test_ready_asset_reuse_returns_same_identity(self):
        first = uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        second = uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        self.assertEqual(first, second)
        self.assertEqual(1, len(SoundDesignAssetStore(self.root).list_assets()['assets']))
        self.assert_staging_released()

    def test_owned_source_size_must_be_an_integer_before_block_reads(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'payload')
        store = SoundDesignAssetStore(self.root)
        for value in (True, '7', 7.0, -1):
            with self.subTest(value=value):
                with patch.object(OwnedSourceUploadStage, 'size', new_callable=PropertyMock,
                                  return_value=value), patch.object(stage, 'iter_blocks') as blocks:
                    with self.assertRaisesRegex(ValueError, 'storage allowance'):
                        store.import_owned_source(stage)
                    blocks.assert_not_called()
        self.assertEqual([], store.list_assets()['assets'])

    def test_hash_phase_rejects_changed_extents_before_reserving_an_asset(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'payload')
        stage.seal()
        store = SoundDesignAssetStore(self.root)
        for blocks, message in (([b'surplus bytes'], 'grew during hashing'),
                                ([b'short'], 'changed during hashing')):
            with self.subTest(message=message):
                with patch.object(stage, 'iter_blocks', return_value=iter(blocks)):
                    with self.assertRaisesRegex(ValueError, message):
                        store.import_owned_source(stage)
                self.assertEqual([], store.list_assets()['assets'])
                self.assertEqual(b'payload', stage.read_bytes())

    def test_inventory_filters_other_directories(self):
        other = self.root / 'other-staging'
        other.mkdir()
        stage = OwnedSourceUploadStage(other, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'payload')
        self.assertEqual(dict(names=set(), bytes=0, count=0),
                         get_upload_stage_inventory(self.staging))
        inventory = get_upload_stage_inventory(other)
        self.assertEqual(1, inventory['count'])
        self.assertEqual(7, inventory['bytes'])
        stage.close()
        self.assertEqual(dict(names=set(), bytes=0, count=0),
                         get_upload_stage_inventory(other))

    def test_retained_and_live_stages_share_the_count_budget(self):
        retained = self.staging / '.source-upload-retained.part'
        retained.write_bytes(b'retained')
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'live')
        stream = io.BytesIO(self.raw)
        with patch.object(uploads, 'MAX_STAGES', 2):
            with self.assertRaisesRegex(ValueError, 'staging is full'):
                uploads.import_sound_asset_upload(self.root, stream)
        self.assertEqual(0, stream.tell())
        self.assertEqual(b'retained', retained.read_bytes())
        self.assertEqual(b'live', stage.read_bytes())
        self.assertEqual(1, get_upload_stage_inventory(self.staging)['count'])

    def test_copy_owned_blocks_rejects_bad_digest_surplus_and_short_writes(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=64)
        self.addCleanup(stage.close)
        stage.write(b'payload')
        stage.seal()
        store = SoundDesignAssetStore(self.root)
        digest = hashlib.sha256(b'payload').hexdigest()
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(ValueError, 'changed during copying'):
                store._copy_owned_blocks(stage, output, '0' * 64, None)
        with tempfile.TemporaryFile() as output:
            with patch.object(stage, 'iter_blocks', return_value=iter([b'surplus bytes'])):
                with self.assertRaisesRegex(ValueError, 'grew during copying'):
                    store._copy_owned_blocks(stage, output, digest, None)
            self.assertEqual(0, output.tell())
        with tempfile.TemporaryFile() as output:
            with patch.object(output, 'write', return_value=0):
                with self.assertRaisesRegex(OSError, 'Incomplete sound source copy'):
                    store._copy_owned_blocks(stage, output, digest, None)

    def test_seal_failure_releases_owned_stage_without_reservation(self):
        with patch.object(OwnedSourceUploadStage, 'seal',
                          side_effect=ValueError('Synthetic seal failure')):
            with self.assertRaisesRegex(ValueError, 'Synthetic seal failure'):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        self.assertEqual([], SoundDesignAssetStore(self.root).list_assets()['assets'])
        self.assert_staging_released()

    def test_owned_source_limit_is_checked_before_hash_or_copy(self):
        stage = OwnedSourceUploadStage(self.staging, byte_limit=len(self.raw))
        self.addCleanup(stage.close)
        stage.write(self.raw)
        store = SoundDesignAssetStore(self.root, source_limit=len(self.raw) - 1)
        with patch.object(stage, 'iter_blocks') as blocks:
            with self.assertRaisesRegex(ValueError, 'storage allowance'):
                store.import_owned_source(stage)
            blocks.assert_not_called()
        self.assertEqual([], store.list_assets()['assets'])

    def test_replaced_or_planted_entry_cannot_affect_a_bystander(self):
        victim = self.root / 'victim.bin'
        victim.write_bytes(b'controlled bytes')
        observed = {}
        real_open = uploads._open_sound_upload_stage

        def attack(directory, available):
            stage = real_open(directory, available)
            entries = list(directory.glob('.source-upload-*.part'))
            observed['entries'] = [path.name for path in entries]
            if entries:
                private = entries[0]
                with self.assertRaises(PermissionError):
                    open(private, 'wb')
                with self.assertRaises(PermissionError):
                    os.unlink(private)
                with self.assertRaises(PermissionError):
                    os.replace(victim, private)
            return stage

        with patch.object(uploads, '_open_sound_upload_stage', side_effect=attack):
            descriptor = uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        if os.name == 'nt':
            self.assertEqual(1, len(observed['entries']))
        else:
            self.assertEqual([], observed['entries'])
        self.assertEqual(b'controlled bytes', victim.read_bytes())
        store = SoundDesignAssetStore(self.root)
        self.assertEqual(self.raw,
            (store.directory / (descriptor['asset_id'] + '.source')).read_bytes())
        self.assert_staging_released()
        planted = self.staging / '.source-upload-planted.part'
        planted.write_bytes(b'p' * 32)
        stream = io.BytesIO(self.raw)
        with patch.object(uploads, 'MAX_STAGING_BYTES', len(self.raw) + 31):
            with self.assertRaisesRegex(ValueError, 'allowance'):
                uploads.import_sound_asset_upload(self.root, stream)
        self.assertEqual(len(self.raw), stream.tell())
        self.assertEqual(b'p' * 32, planted.read_bytes())
        foreign = self.staging / 'foreign.bin'
        foreign.write_bytes(b'unknown')
        stream = io.BytesIO(b'new')
        with self.assertRaisesRegex(ValueError, 'Unknown sound upload files'):
            uploads.import_sound_asset_upload(self.root, stream)
        self.assertEqual(0, stream.tell())
        self.assertEqual(b'unknown', foreign.read_bytes())
        self.assertEqual(b'p' * 32, planted.read_bytes())

    def test_failure_keeps_foreign_entry_and_primary_error(self):
        decoy = self.staging / '.source-upload-decoy.part'
        decoy.write_bytes(b'd' * 32)
        with patch.object(SoundDesignAssetStore, '_copy_owned_blocks',
                          side_effect=ValueError('Synthetic copy failure')):
            with self.assertRaises(ValueError) as caught:
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        self.assertEqual('Synthetic copy failure', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(b'd' * 32, decoy.read_bytes())
        self.assertEqual({decoy.name, 'quota.lock'},
                         {path.name for path in self.staging.iterdir()})
        self.assertEqual((set(), 0), get_upload_stage_accounting(self.staging))
        store = SoundDesignAssetStore(self.root)
        listed = store.list_assets()
        self.assertEqual(1, len(listed['assets']))
        row = listed['assets'][0]
        self.assertEqual('incomplete', row['state'])
        self.assertEqual(len(self.raw), row['source']['bytes'])
        self.assertEqual(0, (store.directory / (row['asset_id'] + '.source')).stat().st_size)
        self.assertEqual(len(self.raw) + MAX_PCM_BYTES, listed['charged_bytes'])

    def test_cleanup_failure_keeps_primary_error(self):
        real_close = uploads._close_sound_upload_stage

        def failing_close(stage):
            self.assertIsNone(real_close(stage))
            return OSError('Synthetic close failure')

        with patch.object(uploads, '_close_sound_upload_stage', side_effect=failing_close), \
                patch.object(SoundDesignAssetStore, 'import_owned_source',
                             side_effect=ValueError('Synthetic store failure')):
            with self.assertRaises(ValueError) as caught:
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        self.assertEqual('Synthetic store failure', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual('Synthetic close failure', str(caught.exception.__cause__))
        self.assert_staging_released()

    def test_ready_import_cleanup_failure_is_loud_and_asset_stays_readable(self):
        real_close = uploads._close_sound_upload_stage

        def failing_close(stage):
            self.assertIsNone(real_close(stage))
            return OSError('Synthetic close failure')

        with patch.object(uploads, '_close_sound_upload_stage', side_effect=failing_close):
            with self.assertRaisesRegex(OSError, 'Synthetic close failure'):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw))
        store = SoundDesignAssetStore(self.root)
        row = store.list_assets()['assets'][0]
        self.assertEqual('ready', row['state'])
        with store.open_canonical(row['asset_id']) as (stream, _):
            self.assertEqual(FRAMES * 4, len(stream.read()))
        self.assert_staging_released()

    def test_cancel_during_decode_retains_incomplete_copy(self):
        cancelled = threading.Event()
        decode = sound_design_decode.decode_sound_design_asset

        def cancelled_decode(*args, **kwargs):
            cancelled.set()
            return decode(*args, **kwargs)

        with patch.object(sound_design_decode, 'decode_sound_design_asset',
                          side_effect=cancelled_decode):
            with self.assertRaises(ExportCancelled):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw),
                                                 cancel_check=cancelled.is_set)
        store = SoundDesignAssetStore(self.root)
        listed = store.list_assets()
        self.assertEqual(1, len(listed['assets']))
        row = listed['assets'][0]
        self.assertEqual('incomplete', row['state'])
        self.assertEqual(self.raw, (store.directory / (row['asset_id'] + '.source')).read_bytes())
        self.assert_staging_released()
        with file_lock(str(self.staging / 'quota'), timeout=0):
            pass

    def test_cancel_between_copy_blocks_retains_partial_source_and_full_charge(self):
        cancelled = threading.Event()
        original = OwnedSourceUploadStage.iter_blocks
        calls = []

        def cancel_after_copy_block(stage, block_size=65536):
            calls.append(stage)
            if len(calls) == 1:
                yield from original(stage, block_size)
                return
            for block in original(stage, 64):
                yield block
                cancelled.set()

        with patch.object(OwnedSourceUploadStage, 'iter_blocks', cancel_after_copy_block):
            with self.assertRaises(AudioGenerationCancelled):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw),
                                                 cancel_check=cancelled.is_set)
        self.assertEqual(2, len(calls))
        store = SoundDesignAssetStore(self.root)
        rows = store.list_assets()['assets']
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual('incomplete', row['state'])
        self.assertEqual(len(self.raw), row['source']['bytes'])
        self.assertEqual(self.raw[:64],
            (store.directory / (row['asset_id'] + '.source')).read_bytes())
        self.assert_staging_released()

    def test_cancel_before_copy_block_retains_charge(self):
        cancelled = threading.Event()
        save = SoundDesignAssetStore._save

        def charge_then_cancel(store, document):
            save(store, document)
            if document['assets'][-1]['state'] == 'incomplete':
                cancelled.set()

        with patch.object(SoundDesignAssetStore, '_save', charge_then_cancel):
            with self.assertRaises(AudioGenerationCancelled):
                uploads.import_sound_asset_upload(self.root, io.BytesIO(self.raw),
                                                 cancel_check=cancelled.is_set)
        store = SoundDesignAssetStore(self.root)
        listed = store.list_assets()
        self.assertEqual(1, len(listed['assets']))
        self.assertEqual('incomplete', listed['assets'][0]['state'])
        self.assert_staging_released()


class SoundDesignStageRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw = synthetic_wav()

    @asynccontextmanager
    async def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            api = FastAPI()
            api.include_router(routes.router)
            with patch.object(routes, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(api),
                                             base_url='http://fixture') as client:
                    yield root, client

    async def test_upload_route_admits_real_audio_without_staging_residue(self):
        async with self.fixture() as (root, client):
            response = await client.post('/api/sound_design/assets/import',
                files={'file': ('effect.wav', self.raw, 'audio/wav')})
            self.assertEqual(200, response.status_code, response.text)
            descriptor = response.json()
            staging = root / uploads.STAGING_DIRECTORY
            self.assertEqual({'quota.lock'}, {path.name for path in staging.iterdir()})
            self.assertEqual((set(), 0), get_upload_stage_accounting(staging))
            store = SoundDesignAssetStore(root)
            self.assertEqual(self.raw,
                (store.directory / (descriptor['asset_id'] + '.source')).read_bytes())

    async def test_route_maps_staging_budget_and_unsafe_entries_to_conflict(self):
        for scenario in ('full', 'unsafe', 'allowance'):
            with self.subTest(scenario=scenario):
                async with self.fixture() as (root, client):
                    staging = root / uploads.STAGING_DIRECTORY
                    staging.mkdir()
                    retained = staging / '.source-upload-retained.part'
                    retained.write_bytes(b'preserve')
                    if scenario == 'unsafe':
                        (staging / 'foreign-directory').mkdir()
                    setting = 'MAX_STAGES' if scenario == 'full' else 'MAX_STAGING_BYTES'
                    value = 1 if scenario == 'full' else len(b'preserve') + 1
                    with patch.object(uploads, setting, value):
                        response = await client.post('/api/sound_design/assets/import',
                            files={'file': ('effect.wav', self.raw, 'audio/wav')})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual([], SoundDesignAssetStore(root).list_assets()['assets'])
                    self.assertEqual(b'preserve', retained.read_bytes())
                    self.assertEqual((set(), 0), get_upload_stage_accounting(staging))
                    if scenario == 'unsafe':
                        self.assertTrue((staging / 'foreign-directory').is_dir())
                    self.assertEqual({retained.name, 'quota.lock'} |
                        ({'foreign-directory'} if scenario == 'unsafe' else set()),
                        {path.name for path in staging.iterdir()})

    async def test_empty_upload_is_refused_before_reservation(self):
        async with self.fixture() as (root, client):
            response = await client.post('/api/sound_design/assets/import',
                files={'file': ('empty.wav', b'', 'audio/wav')})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual([], SoundDesignAssetStore(root).list_assets()['assets'])
            staging = root / uploads.STAGING_DIRECTORY
            self.assertEqual({'quota.lock'}, {path.name for path in staging.iterdir()})
            self.assertEqual((set(), 0), get_upload_stage_accounting(staging))
