"""Source hashing cannot wait for payload from a nonregular filesystem node."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from chapter_timing_cache import read_source_identity
from tts import ExportCancelled


class ChapterSourceIdentityTests(unittest.TestCase):
    def test_regular_empty_and_followed_alias_keep_the_existing_identity_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.wav'
            for payload in (b'', b'source bytes' * 10000):
                source.write_bytes(payload)
                result = read_source_identity(source, lambda: False)
                self.assertEqual({'version', 'sha256'}, set(result))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), result['sha256'])
            if os.name != 'nt':
                alias = Path(tmp) / 'alias.wav'
                alias.symlink_to(source)
                self.assertEqual(result, read_source_identity(alias, lambda: False))

    def test_missing_directory_and_cancellation_refuse_without_payload_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_source_identity(Path(tmp) / 'missing', lambda: False))
            self.assertIsNone(read_source_identity(tmp, lambda: False))
            with self.assertRaises(ExportCancelled):
                read_source_identity(Path(tmp) / 'missing', lambda: True)

    def test_fifo_and_regular_to_fifo_open_race_have_a_hard_process_bound(self):
        code = '''
import os,tempfile
from pathlib import Path
from unittest.mock import patch
import chapter_timing_cache as cache
from project import ProjectManager
with tempfile.TemporaryDirectory() as tmp:
    source = Path(tmp) / 'source.wav'
    if hasattr(os, 'mkfifo'):
        os.mkfifo(source)
        assert cache.read_source_identity(source, lambda:False) is None
        manager = ProjectManager(tmp)
        assert manager.get_chunk_audio_segment({'audio_path':source.name}) is None
        source.unlink()
        source.write_bytes(b'regular before open')
        original, descriptors = os.open, []
        def replace_before_open(path,*args,**kwargs):
            if Path(path) == source:
                source.unlink()
                os.mkfifo(source)
            fd = original(path,*args,**kwargs)
            descriptors.append(fd)
            return fd
        with patch.object(cache.os,'open',side_effect=replace_before_open):
            assert cache.read_source_identity(source,lambda:False) is None
        assert descriptors
        try:
            os.fstat(descriptors[0])
        except OSError:
            pass
        else:
            raise AssertionError('source descriptor leaked')
    else:
        assert cache.read_source_identity(tmp,lambda:False) is None
print('nonregular source checks complete')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
            text=True, timeout=20, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('nonregular source checks complete', result.stdout)
