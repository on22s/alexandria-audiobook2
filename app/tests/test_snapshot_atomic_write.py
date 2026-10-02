import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import utils
from update_api_contract_snapshots import write_json_snapshot


class SnapshotAtomicWriteTests(unittest.TestCase):
    def test_snapshot_bytes_preserve_deterministic_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'contracts', 'snapshot.json')
            data = {'z': [{'b': 2, 'a': '雪'}], 'a': True}
            write_json_snapshot(path, data)
            self.assertEqual((json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)+'\n').encode(), path.read_bytes())

    def test_failed_replacement_preserves_old_snapshot_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'snapshot.json')
            original = b'{"old": true}\n'
            path.write_bytes(original)
            with patch.object(utils.os, 'replace', side_effect=OSError(errno.EIO, 'fixture replacement failed')):
                with self.assertRaises(OSError):
                    write_json_snapshot(path, {'new': True})
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_partial_serialization_failure_preserves_old_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'snapshot.json')
            original = b'{"old": true}\n'
            path.write_bytes(original)
            def fail_dump(data, stream, **kwargs):
                stream.write('{"partial":')
                raise OSError(errno.ENOSPC, 'fixture disk full')
            with patch.object(utils.json, 'dump', side_effect=fail_dump):
                with self.assertRaises(OSError):
                    write_json_snapshot(path, {'new': True})
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))
