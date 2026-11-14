"""Profiler checkpoints use the common durable writer and preserve prior data on failure."""
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import utils
from tests.test_voicelab_pipeline_scripts import voice_profiler


class ProfilerSharedWriterTests(unittest.TestCase):
    def test_common_writer_fsyncs_before_replace_and_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.json'
            events = []
            real_fsync, real_replace = utils.os.fsync, utils.os.replace
            real_open, real_close = utils.os.open, utils.os.close
            directory_fd = object()
            def open_directory(name, flags, *args, **kwargs):
                if os.name == 'nt' and str(name) == str(path.parent):
                    return directory_fd
                return real_open(name, flags, *args, **kwargs)
            def close(fd):
                if fd is not directory_fd:
                    return real_close(fd)
            def sync(fd):
                events.append('sync')
                if fd is not directory_fd:
                    return real_fsync(fd)
            def replace(source, target):
                self.assertTrue(events, 'checkpoint was not synced before replacement')
                self.assertEqual('sync', events[-1])
                events.append('replace')
                return real_replace(source, target)
            data = [{'id': '日本語', 'voice_profile': '暖かい声'}]
            # Supply directory-fsync capability only on Windows. POSIX still opens
            # and syncs a real directory descriptor to exercise its durability.
            with patch.object(utils.os, 'open', side_effect=open_directory), \
                 patch.object(utils.os, 'close', side_effect=close), \
                 patch.object(utils.os, 'fsync', side_effect=sync), \
                 patch.object(utils.os, 'replace', side_effect=replace):
                voice_profiler.atomic_json_write(data, str(path))
            self.assertEqual(data, json.loads(path.read_text()))
            self.assertEqual(['sync','replace','sync'], events)
            self.assertIs(utils.atomic_json_write, voice_profiler.atomic_json_write)
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_failed_checkpoint_preserves_previous_manifest_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.json'
            previous = b'[{"id":"previous"}]'
            path.write_bytes(previous)
            with patch.object(utils.os, 'replace', side_effect=OSError(errno.ENOSPC, 'fixture disk full')):
                with self.assertRaises(OSError):
                    voice_profiler.atomic_json_write([{'id':'new'}], str(path))
            self.assertEqual(previous, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))
