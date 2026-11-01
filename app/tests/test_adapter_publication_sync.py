"""Adapter publication flushes existing bytes and never hides durability faults."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import adapter_publication as publication


class AdapterTreeSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='adapter-sync-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'nested').mkdir()
        (self.root / 'weights.bin').write_bytes(bytes(range(256)))
        (self.root / 'nested' / 'config.json').write_bytes(b'{"rank":8}\n')
        (self.root / 'nested' / 'empty.bin').write_bytes(b'')
        self.before = self.get_bytes()

    def get_bytes(self):
        return {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def test_native_tree_sync_preserves_every_file_and_closes_handles(self):
        publication.sync_adapter_tree(str(self.root))
        self.assertEqual(self.before, self.get_bytes())
        for path in self.before:
            renamed = path.with_name(path.name + '.renamed')
            path.rename(renamed)
            renamed.rename(path)

    def test_each_platform_uses_non_truncating_access_and_flushes_every_file(self):
        real_open, real_fsync = open, os.fsync
        for platform_name in ('nt', 'posix'):
            with self.subTest(platform=platform_name):
                streams, flushed = {}, []

                def tracked_open(filename, mode):
                    stream = real_open(filename, mode)
                    streams[stream.fileno()] = (Path(filename), stream)
                    return stream

                def checked_fsync(descriptor):
                    filename, stream = streams[descriptor]
                    self.assertEqual(platform_name == 'nt', stream.writable())
                    self.assertTrue(stream.readable())
                    self.assertEqual(self.before[filename], stream.read())
                    if platform_name == os.name:
                        real_fsync(descriptor)
                    flushed.append(filename)

                platform_os = SimpleNamespace(name=platform_name, walk=os.walk,
                                              path=os.path, fsync=checked_fsync)
                with patch.object(publication, 'os', platform_os), patch.object(
                        publication, 'open', tracked_open, create=True), patch.object(
                        publication, 'sync_adapter_directory') as sync_directory:
                    publication.sync_adapter_tree(str(self.root))
                self.assertCountEqual(self.before, flushed)
                self.assertEqual([str(self.root / 'nested'), str(self.root)],
                                 [call.args[0] for call in sync_directory.call_args_list])
                self.assertTrue(all(stream.closed for _, stream in streams.values()))
                self.assertEqual(self.before, self.get_bytes())

    def test_file_flush_failure_propagates_without_changing_bytes(self):
        real_fsync = os.fsync
        flushed = []

        def fail_second_file(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                flushed.append(descriptor)
                if len(flushed) == 2:
                    raise OSError('adapter flush fault')
            return real_fsync(descriptor)

        with patch.object(publication.os, 'fsync', side_effect=fail_second_file):
            with self.assertRaisesRegex(OSError, 'adapter flush fault'):
                publication.sync_adapter_tree(str(self.root))
        self.assertEqual(2, len(flushed))
        self.assertEqual(self.before, self.get_bytes())

    def test_open_failure_propagates_without_changing_bytes(self):
        with patch.object(publication, 'open', side_effect=PermissionError('adapter access'),
                          create=True):
            with self.assertRaisesRegex(PermissionError, 'adapter access'):
                publication.sync_adapter_tree(str(self.root))
        self.assertEqual(self.before, self.get_bytes())

    def test_directory_flush_failure_propagates_without_changing_bytes(self):
        with patch.object(publication, 'sync_adapter_directory',
                          side_effect=OSError('directory flush fault')):
            with self.assertRaisesRegex(OSError, 'directory flush fault'):
                publication.sync_adapter_tree(str(self.root))
        self.assertEqual(self.before, self.get_bytes())
