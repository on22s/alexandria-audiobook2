import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project import ProjectManager
from utils import atomic_json_write


class ProjectUidCacheTests(unittest.TestCase):
    def test_replaced_book_gets_persisted_uids_and_retains_them_after_edits(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            path = Path(manager.chunks_path)
            atomic_json_write([{'id': 0, 'text': 'First book'}], path)
            first = manager.load_chunks()[0]['uid']
            atomic_json_write([{'id': 0, 'text': 'Second book'},
                               {'id': 1, 'text': 'Second line'}], path)
            chunks = manager.load_chunks()
            uids = [row.get('uid') for row in chunks]
            self.assertTrue(all(uids))
            self.assertEqual(2, len(set(uids)))
            self.assertNotIn(first, uids)
            self.assertEqual(chunks, json.loads(path.read_text()))
            manager.insert_chunk(0)
            after = manager.load_chunks()
            self.assertEqual(uids, [after[0]['uid'], after[2]['uid']])
            manager.delete_chunk(1)
            self.assertEqual(uids, [row['uid'] for row in manager.load_chunks()])

    def test_public_save_invalidates_without_mutating_the_supplied_rows(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            manager.save_chunks([{'id': 0, 'uid': 'old', 'text': 'Old'}])
            manager.load_chunks()
            replacement = [{'id': 0, 'text': 'Replacement'}]
            original = copy.deepcopy(replacement)
            manager.save_chunks(replacement)
            loaded = manager.load_chunks()
            self.assertTrue(loaded[0].get('uid'))
            self.assertEqual(original, replacement)
            self.assertEqual(loaded, json.loads(Path(manager.chunks_path).read_text()))

    def test_in_place_replacement_with_same_size_and_mtime_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            path = Path(manager.chunks_path)
            path.write_text('[{"id":0,"uid":"abc","text":"A"}]')
            manager.load_chunks()
            before = path.stat()
            path.write_text('[{"id":0,"uid":"","text":"ABCD"}]')
            self.assertEqual(before.st_size, path.stat().st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertTrue(manager.load_chunks()[0].get('uid'))

    def test_status_updates_keep_the_cache_and_single_row_uid_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            manager.save_chunks([{'id': i, 'text': str(i)} for i in range(100)])
            original = manager.load_chunks()
            with patch('project._new_chunk_uid', side_effect=AssertionError('unexpected backfill')):
                for _ in range(3):
                    manager._update_chunk_fields(0, status='generating')
                    manager._update_chunk_fields_by_uid(original[1]['uid'], status='done')
                    self.assertEqual([r['uid'] for r in original],
                                     [r['uid'] for r in manager.load_chunks()])
            from project import safe_load_json
            uid_reads = []

            class TrackedRow(dict):
                def get(self, key, *args):
                    if key == 'uid':
                        uid_reads.append(key)
                    return super().get(key, *args)

            def read_tracked(path):
                return [TrackedRow(row) for row in safe_load_json(path)]

            with patch('project.safe_load_json', side_effect=read_tracked):
                for _ in range(3):
                    manager.load_chunks()
            self.assertEqual([], uid_reads, 'unchanged files should bypass UID scans')
            manager._update_chunk_fields(0, uid='')
            self.assertTrue(manager.load_chunks()[0].get('uid'))


if __name__ == '__main__':
    unittest.main()
