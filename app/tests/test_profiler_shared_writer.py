"""Profiler checkpoints use the common durable writer and preserve prior data on failure."""
import errno
import json
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
            def sync(fd):
                events.append('sync')
                return real_fsync(fd)
            def replace(source, target):
                self.assertTrue(events, 'checkpoint was not synced before replacement')
                self.assertEqual('sync', events[-1])
                events.append('replace')
                return real_replace(source, target)
            data = [{'id': '日本語', 'voice_profile': '暖かい声'}]
            with patch.object(utils.os, 'fsync', side_effect=sync), patch.object(utils.os, 'replace', side_effect=replace):
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
