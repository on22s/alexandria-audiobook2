"""Real filesystem controls for complete-file upload publication."""
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import source_upload_publication as publication


class SourceUploadPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = Path(publication.create_source_upload_stage(self.root))
        self.payload = b'Complete synthetic source.\n' * 1000
        self.stage.write_bytes(self.payload)

    def get_companion_bytes(self):
        # Synthetic valid provenance; EPUB parsing is exercised by route tests.
        digest = hashlib.sha256(self.payload).hexdigest()
        origin = hashlib.sha256(b'Synthetic archive fixture').hexdigest()
        identity = hashlib.sha256(f'AA2_epub_publication_v1:{origin}:{digest}'.encode()).hexdigest()
        return json.dumps({'version': 1, 'binding': {'kind': 'text', 'sha256': digest},
            'candidates': [{'version': 1, 'id': identity,
                'origin': {'kind': 'epub', 'filename': 'book.epub', 'sha256': origin, 'text_sha256': digest},
                'fields': {'title': [], 'author': []}, 'other_creators': [],
                'metadata_status': 'unavailable', 'cover': None, 'cover_status': 'not_declared',
                'warnings': ['Synthetic provenance fixture']}]}).encode()

    def publish(self, callback=lambda path: None):
        return publication.publish_completed_upload(
            self.stage, 'book.txt', callback, limit=len(self.payload))

    def test_visibility_handoff_has_all_bytes_and_private_name_is_removed(self):
        observed = []
        result = self.publish(lambda path: observed.append(Path(path).read_bytes()))
        self.assertEqual([self.payload], observed)
        self.assertEqual(self.payload, Path(result).read_bytes())
        self.assertEqual([Path(result)], list(self.root.iterdir()))
        self.assertEqual(1, Path(result).stat().st_nlink)

    def test_collisions_preserve_existing_file_directory_and_orphan_provenance(self):
        (self.root / 'book.txt').write_bytes(b'Prior source')
        (self.root / 'book_2.txt').mkdir()
        (self.root / 'book_3.txt.publication.json').write_bytes(b'Prior provenance')
        result = self.publish()
        self.assertEqual('book_4.txt', Path(result).name)
        self.assertEqual(b'Prior source', (self.root / 'book.txt').read_bytes())
        self.assertTrue((self.root / 'book_2.txt').is_dir())
        self.assertEqual(b'Prior provenance', (self.root / 'book_3.txt.publication.json').read_bytes())

    def test_late_sync_error_preserves_complete_public_source_and_handoff(self):
        visible = []
        with patch.object(publication, 'sync_adapter_directory', side_effect=OSError('sync fixture')):
            with self.assertRaises(OSError):
                self.publish(visible.append)
        self.assertEqual([str(self.root / 'book.txt')], visible)
        self.assertEqual(self.payload, Path(visible[0]).read_bytes())

    def test_unsupported_move_never_copies_or_removes_recoverable_stage(self):
        visible = []
        with patch.object(publication, 'move_source_upload_exclusively', side_effect=OSError(errno.EOPNOTSUPP, 'fixture')):
            with self.assertRaises(OSError):
                self.publish(visible.append)
        self.assertEqual([], visible)
        self.assertEqual([self.stage], list(self.root.iterdir()))
        self.assertEqual(self.payload, self.stage.read_bytes())

    def test_size_limit_refuses_before_publication(self):
        with self.assertRaises(ValueError):
            publication.publish_completed_upload(self.stage, 'book.txt', lambda path: None,
                                                 limit=len(self.payload) - 1)
        self.assertEqual([self.stage], list(self.root.iterdir()))

    def test_source_flush_has_write_access_without_changing_bytes(self):
        import stat
        original = os.fsync
        checked = []
        def require_write_access(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                # A zero-byte write checks access without changing source data.
                os.write(descriptor, b'')
                checked.append(descriptor)
            return original(descriptor)
        with patch.object(publication.os, 'fsync', side_effect=require_write_access):
            result = self.publish()
        self.assertEqual(1, len(checked))
        self.assertEqual(self.payload, Path(result).read_bytes())

    def test_unsafe_destination_names_never_escape_stage_directory(self):
        for name in ('../outside.txt', '/outside.txt', 'C:\\outside.txt', '.private.txt', 'book.epub'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                publication.publish_completed_upload(self.stage, name, lambda path: None,
                                                     limit=len(self.payload))
        self.assertEqual([self.stage], list(self.root.iterdir()))

    def test_companion_is_complete_before_source_becomes_visible(self):
        companion_bytes = self.get_companion_bytes()
        observed = []
        def visible(path):
            observed.append((Path(path).read_bytes(), Path(path + '.publication.json').read_bytes()))
        result = publication.publish_completed_upload(
            self.stage, 'book.txt', visible, limit=len(self.payload),
            companion_bytes=companion_bytes)
        self.assertEqual([(self.payload, companion_bytes)], observed)
        self.assertEqual({Path(result).name, Path(result).name + '.publication.json'},
                         {path.name for path in self.root.iterdir()})

    def test_failed_source_install_removes_only_owned_companion_and_retains_stage(self):
        companion_bytes = self.get_companion_bytes()
        with patch.object(publication, 'move_source_upload_exclusively', side_effect=OSError('install fixture')):
            with self.assertRaises(OSError):
                publication.publish_completed_upload(
                    self.stage, 'book.txt', lambda path: self.fail('unexpected publication'),
                    limit=len(self.payload), companion_bytes=companion_bytes)
        self.assertEqual([self.stage], list(self.root.iterdir()))
        self.assertEqual(self.payload, self.stage.read_bytes())

    def test_destination_created_during_install_is_never_overwritten(self):
        original = publication.move_source_upload_exclusively
        calls = []
        def collide(source, destination, **options):
            calls.append(Path(destination).name)
            if len(calls) == 1:
                Path(destination).write_bytes(b'Other owner source')
            return original(source, destination, **options)
        with patch.object(publication, 'move_source_upload_exclusively', side_effect=collide):
            result = self.publish()
        self.assertEqual(['book.txt', 'book_2.txt'], calls)
        self.assertEqual(b'Other owner source', (self.root / 'book.txt').read_bytes())
        self.assertEqual(self.payload, Path(result).read_bytes())

    def test_invalid_companion_does_not_publish_source_or_metadata(self):
        with self.assertRaises(ValueError):
            publication.publish_completed_upload(
                self.stage, 'book.txt', lambda path: self.fail('unexpected publication'),
                limit=len(self.payload), companion_bytes=b'{}')
        self.assertEqual([self.stage], list(self.root.iterdir()))

    def test_process_death_after_install_leaves_single_link_complete_source(self):
        import subprocess
        import sys
        child = """import os, sys
from pathlib import Path
import source_upload_publication as publication
root = Path(sys.argv[1])
stage = Path(publication.create_source_upload_stage(root))
stage.write_bytes(b'Complete synthetic source.')
if sys.argv[2] == 'sync':
    publication.sync_adapter_directory = lambda path: os._exit(71)
publication.publish_completed_upload(stage, 'book.txt',
    lambda path: os._exit(71) if sys.argv[2] == 'visible' else None, limit=1024)
"""
        for boundary in ('visible', 'sync'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', child, directory, boundary],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(71, result.returncode, result.stderr)
                public = Path(directory) / 'book.txt'
                self.assertEqual(b'Complete synthetic source.', public.read_bytes())
                self.assertEqual(1, public.stat().st_nlink)
                self.assertEqual([public], list(Path(directory).iterdir()))

    def test_native_call_signatures_and_collision_errors_for_unix_platforms(self):
        import ctypes
        from types import SimpleNamespace
        from unittest.mock import Mock
        for platform, symbol, arguments in (
                ('linux', 'renameat2', (-100, b'stage', -100, b'book.txt', 1)),
                ('darwin', 'renamex_np', (b'stage', b'book.txt', 4))):
            with self.subTest(platform=platform):
                operation = Mock(return_value=-1)
                library = SimpleNamespace(**{symbol: operation})
                with patch.object(publication, 'os', SimpleNamespace(name='posix', fsencode=os.fsencode)), \
                     patch.object(publication.sys, 'platform', platform), \
                     patch.object(ctypes, 'CDLL', return_value=library), \
                     patch.object(ctypes, 'get_errno', return_value=errno.EEXIST):
                    with self.assertRaises(FileExistsError):
                        publication.move_source_upload_exclusively('stage', 'book.txt')
                operation.assert_called_once_with(*arguments)
                self.assertEqual(ctypes.c_int, operation.restype)
                self.assertEqual(len(arguments), len(operation.argtypes))

    def test_missing_native_exclusive_move_refuses_without_unsafe_fallback(self):
        import ctypes
        from types import SimpleNamespace
        with patch.object(publication, 'os', SimpleNamespace(name='posix', fsencode=os.fsencode)), \
             patch.object(publication.sys, 'platform', 'linux'), \
             patch.object(ctypes, 'CDLL', return_value=SimpleNamespace()):
            with self.assertRaises(OSError) as caught:
                publication.move_source_upload_exclusively('stage', 'book.txt')
        self.assertEqual(errno.ENOTSUP, caught.exception.errno)

    def test_windows_boundary_uses_existing_no_overwrite_rename(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        rename = Mock(side_effect=FileExistsError('destination exists'))
        with patch.object(publication, 'os', SimpleNamespace(name='nt', rename=rename)):
            with self.assertRaises(FileExistsError):
                publication.move_source_upload_exclusively('stage', 'book.txt')
        rename.assert_called_once_with('stage', 'book.txt')

    def test_stream_allocation_failure_closes_raw_descriptor_and_owned_companion(self):
        descriptors = []
        def failed_wrapper(descriptor, *args, **kwargs):
            descriptors.append(descriptor)
            raise MemoryError('synthetic buffered stream allocation failure')
        try:
            with patch.object(publication.os, 'fdopen', side_effect=failed_wrapper):
                with self.assertRaises(MemoryError):
                    publication.publish_completed_upload(
                        self.stage, 'book.txt', lambda path: self.fail('unexpected publication'),
                        limit=len(self.payload), companion_bytes=self.get_companion_bytes())
            self.assertEqual(1, len(descriptors))
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptors[0])
            self.assertEqual(errno.EBADF, closed.exception.errno)
            self.assertEqual([self.stage], list(self.root.iterdir()))
            self.assertEqual(self.payload, self.stage.read_bytes())
        finally:
            # Bound resources even when this test rejects an older implementation.
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_pre_source_process_death_preserves_unpaired_files_and_retry_skips_orphan(self):
        import subprocess
        import sys
        child = """import os, sys
from pathlib import Path
import source_upload_publication as publication
root = Path(sys.argv[1])
stage = Path(publication.create_source_upload_stage(root))
stage.write_bytes(b'Complete synthetic source.\\n' * 1000)
if sys.argv[2] == 'companion_created':
    publication.os.fdopen = lambda *args, **kwargs: os._exit(72)
else:
    publication.move_source_upload_exclusively = lambda *args: os._exit(72)
publication.publish_completed_upload(stage, 'book.txt', lambda path: None,
    limit=1024 * 1024, companion_bytes=sys.argv[3].encode())
"""
        for boundary in ('companion_created', 'companion_complete'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', child, directory, boundary,
                                         self.get_companion_bytes().decode()],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(72, result.returncode, result.stderr)
                root = Path(directory)
                self.assertFalse((root / 'book.txt').exists())
                retained = {path.name: path.read_bytes() for path in root.iterdir()}
                self.assertEqual(2, len(retained))
                self.assertEqual(b'' if boundary == 'companion_created' else self.get_companion_bytes(),
                                 retained['book.txt.publication.json'])
                self.assertTrue(all(Path(name).suffix not in ('.txt', '.md') for name in retained))
                stage = Path(publication.create_source_upload_stage(root))
                stage.write_bytes(b'New complete retry source.')
                installed = publication.publish_completed_upload(
                    stage, 'book.txt', lambda path: None, limit=1024)
                self.assertEqual('book_2.txt', Path(installed).name)
                self.assertEqual(b'New complete retry source.', Path(installed).read_bytes())
                self.assertEqual(retained, {name: (root / name).read_bytes() for name in retained})
