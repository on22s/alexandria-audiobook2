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
