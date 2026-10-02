"""Native upload versions must not accumulate unbounded cached digests."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from routers import script


class UploadHashCacheTests(unittest.TestCase):
    def setUp(self):
        with script._upload_hash_lock:
            self.saved = script._upload_hash_cache.copy()
            script._upload_hash_cache.clear()
        self.addCleanup(self.restore_cache)

    def restore_cache(self):
        with script._upload_hash_lock:
            script._upload_hash_cache.clear()
            script._upload_hash_cache.update(self.saved)

    def test_native_versions_replace_historical_digest_for_the_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            for index in range(24):
                data = b'source' * (index + 1)
                path.write_bytes(data)
                self.assertEqual(hashlib.sha256(data).hexdigest(), script._get_upload_hash(str(path)))
                self.assertEqual(1, len(script._upload_hash_cache))

    def test_capacity_and_access_recency_evict_oldest_without_changing_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(script, 'MAX_UPLOAD_HASH_CACHE_ENTRIES', 3, create=True):
            paths = [Path(tmp) / f'{index}.txt' for index in range(4)]
            for index, path in enumerate(paths):
                path.write_bytes(bytes([index]) * (index + 1))
            for path in paths[:3]:
                script._get_upload_hash(str(path))
            # A hit refreshes recency without reopening the native file.
            with patch('builtins.open', side_effect=AssertionError('cache hit reopened file')):
                script._get_upload_hash(str(paths[0]))
            script._get_upload_hash(str(paths[3]))
            self.assertEqual(3, len(script._upload_hash_cache))
            self.assertEqual([str(paths[2]), str(paths[0]), str(paths[3])], list(script._upload_hash_cache))
            real_open = open
            opened = []
            def observe(path, *args, **kwargs):
                opened.append(str(path))
                return real_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=observe):
                digest = script._get_upload_hash(str(paths[1]))
            self.assertEqual([str(paths[1])], opened)
            self.assertEqual(hashlib.sha256(b'\x01\x01').hexdigest(), digest)
            self.assertEqual(3, len(script._upload_hash_cache))
            for index, path in enumerate(paths):
                self.assertEqual(bytes([index]) * (index + 1), path.read_bytes())
