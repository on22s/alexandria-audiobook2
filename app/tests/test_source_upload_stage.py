"""Real filesystem contracts for descriptor-owned upload payloads."""
import os
from pathlib import Path
import tempfile
import tracemalloc
import sys
import unittest
from unittest.mock import patch

from source_upload_stage import OwnedSourceUploadStage


class OwnedSourceUploadStageTests(unittest.TestCase):
    def test_small_payload_capture_does_not_allocate_the_configured_maximum(self):
        stage = self.stage(16 * 1024**2)
        stage.write(b'x')
        tracemalloc.start()
        try:
            self.assertEqual(b'x', stage.read_bytes())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 256 * 1024, 'Capturing a tiny payload allocated the stage limit')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / 'uploads'
        self.root.mkdir()

    def stage(self, limit=64):
        result = OwnedSourceUploadStage(self.root, byte_limit=limit)
        self.addCleanup(result.close)
        return result

    def test_bounded_write_and_abandoned_cleanup(self):
        stage = self.stage(8)
        stage.write(b'complete')
        with self.assertRaises(ValueError):
            stage.write(b'x')
        self.assertEqual(b'complete', stage.read_bytes())
        stage.close()
        self.assertEqual([], list(self.root.iterdir()))

    def test_stream_allocation_failure_leaves_no_private_stage(self):
        operation = ('source_upload_stage.tempfile.TemporaryFile' if sys.platform == 'darwin'
                     else 'source_upload_stage.os.fdopen')
        with patch(operation, side_effect=OSError('controlled allocation failure')):
            with self.assertRaisesRegex(OSError, 'controlled allocation failure'):
                self.stage()
        self.assertEqual([], list(self.root.iterdir()))

    def test_posix_root_is_revalidated_after_native_stage_creation(self):
        if os.name == 'nt':
            return
        with patch.object(OwnedSourceUploadStage, '_check_root',
                          side_effect=[None, ValueError('controlled root replacement')]):
            with self.assertRaisesRegex(ValueError, 'controlled root replacement'):
                self.stage()
        self.assertEqual([], list(self.root.iterdir()))

    def test_complete_publication_refuses_collision_and_preserves_foreign_bytes(self):
        (self.root / 'book.txt').write_bytes(b'foreign')
        stage = self.stage()
        stage.write(b'complete payload')
        stage.seal()
        with self.assertRaises(FileExistsError):
            stage.publish('book.txt')
        self.assertEqual(b'foreign', (self.root / 'book.txt').read_bytes())
        result = stage.publish('book-2.txt')
        self.assertEqual(str(self.root / 'book-2.txt'), result)
        self.assertEqual(b'complete payload', Path(result).read_bytes())
        self.assertEqual(1, Path(result).stat().st_nlink)
        stage.sync_directory()
        stage.close()
        self.assertEqual(['book-2.txt', 'book.txt'], sorted(p.name for p in self.root.iterdir()))

    def test_sealed_payload_cannot_be_extended(self):
        stage = self.stage()
        stage.write(b'original')
        stage.seal()
        with self.assertRaises(ValueError):
            stage.write(b'changed')
        result = stage.publish('book.txt')
        self.assertEqual(b'original', Path(result).read_bytes())

    def test_invalid_destinations_cannot_escape_storage_or_address_device_names(self):
        stage = self.stage()
        stage.write(b'original')
        for name in ('../outside.txt', 'sub/file.txt', 'stream:alias', 'NUL.txt', '.hidden', 'tail.'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                stage.publish(name)
        stage.close()
        self.assertEqual([], list(self.root.iterdir()))
        self.assertFalse((self.parent / 'outside.txt').exists())

    def test_later_close_preserves_published_payload(self):
        stage = self.stage()
        stage.write(b'complete payload')
        result = stage.publish('book.txt')
        self.assertTrue(stage.publication_started)
        stage.close()
        stage.close()
        self.assertEqual(result, stage.published_path)
        self.assertEqual(b'complete payload', Path(result).read_bytes())

    def test_changed_storage_cannot_redirect_publication(self):
        stage = self.stage()
        stage.write(b'original')
        retained = self.parent / 'retained'
        if os.name == 'nt':
            with self.assertRaises(PermissionError):
                self.root.rename(retained)
        else:
            self.root.rename(retained)
            self.root.mkdir()
            (self.root / 'foreign').write_bytes(b'preserve')
            with self.assertRaises(ValueError):
                stage.publish('book.txt')
            self.assertEqual(b'preserve', (self.root / 'foreign').read_bytes())
            self.assertFalse((self.root / 'book.txt').exists())
        stage.close()

    def test_windows_alias_added_during_rename_preserves_only_external_link(self):
        if os.name != 'nt':
            return
        stage = self.stage()
        stage.write(b'original')
        original = stage._rename_windows
        alias = self.root / 'external'
        def add_alias(destination):
            original(destination)
            os.link(destination, alias)
        with patch.object(stage, '_rename_windows', side_effect=add_alias):
            with self.assertRaisesRegex(ValueError, 'alias'):
                stage.publish('book.txt')
        self.assertFalse((self.root / 'book.txt').exists())
        self.assertEqual(b'original', alias.read_bytes())
        self.assertEqual(1, alias.stat().st_nlink)

    def test_windows_alias_before_validation_is_refused_without_deleting_external_link(self):
        if os.name != 'nt':
            return
        stage = self.stage()
        stage.write(b'original')
        stage._stream.flush()
        private = next(self.root.glob('.source-upload-*.part'))
        alias = self.root / 'external'
        os.link(private, alias)
        with self.assertRaises(ValueError):
            stage.seal()
        stage.close()
        self.assertFalse(private.exists())
        self.assertEqual(b'original', alias.read_bytes())

    def test_block_iteration_yields_the_sealed_extent_without_closing(self):
        stage = self.stage(256)
        payload = b'0123456789' * 16
        stage.write(payload)
        blocks = list(stage.iter_blocks(64))
        self.assertEqual([64, 64, 32], [len(block) for block in blocks])
        self.assertEqual(payload, b''.join(blocks))
        self.assertFalse(stage._stream.closed)
        self.assertEqual(payload, stage.read_bytes())
        self.assertEqual([payload], list(stage.iter_blocks(1024)))

    def test_block_iteration_arguments_are_bounded(self):
        stage = self.stage()
        stage.write(b'payload')
        for value in (True, False, 63, 1024**2 + 1, 0, -1, 1.5, '64', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'block size'):
                list(stage.iter_blocks(value))
        self.assertEqual([b'payload'], list(stage.iter_blocks(1024**2)))
        stage.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            list(stage.iter_blocks())

    def test_block_iteration_detects_a_changed_extent(self):
        stage = self.stage(256)
        stage.write(b'a' * 128)
        stage.seal()
        stage._stream.seek(0)
        stage._stream.write(b'b' * 128)
        stage._stream.flush()
        with self.assertRaisesRegex(ValueError, 'changed while reading'):
            list(stage.iter_blocks(64))
        stage.close()
        stage = self.stage(256)
        stage.write(b'a' * 192)
        stage.seal()
        blocks = stage.iter_blocks(64)
        self.assertEqual(b'a' * 64, next(blocks))
        self.assertEqual(b'a' * 64, next(blocks))
        self.assertEqual(b'a' * 64, next(blocks))
        stage._stream.seek(184)
        stage._stream.write(b'b' * 8)
        stage._stream.flush()
        with self.assertRaisesRegex(ValueError, 'changed while reading'):
            next(blocks)

    def test_block_iteration_rechecks_root_after_the_final_block(self):
        stage = self.stage(256)
        stage.write(b'payload')
        stage.seal()
        with patch.object(stage, '_check_root',
                          side_effect=[None, ValueError('Synthetic late root change')]) as check:
            blocks = stage.iter_blocks(64)
            self.assertEqual(b'payload', next(blocks))
            with self.assertRaisesRegex(ValueError, 'Synthetic late root change'):
                next(blocks)
        self.assertEqual(2, check.call_count)

    def test_block_iteration_checks_root_after_sealing(self):
        stage = self.stage(256)
        stage.write(b'payload')
        stage.seal()
        retained = self.root.with_name(self.root.name + '-retained')
        if os.name == 'nt':
            with self.assertRaises(PermissionError):
                self.root.rename(retained)
            self.assertEqual(b'payload', b''.join(stage.iter_blocks()))
        else:
            self.root.rename(retained)
            try:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    list(stage.iter_blocks())
            finally:
                retained.rename(self.root)
