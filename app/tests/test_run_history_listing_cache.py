import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_history as history
from utils import atomic_json_write


class RunHistoryListingCacheTests(unittest.TestCase):
    def make_history(self, root, count=80):
        for index in range(count):
            # Reverse filename order relative to start time; mtime is equal.
            run_id = f'run_{count - index:04d}'
            path = Path(root, run_id + '.json')
            atomic_json_write({'id': run_id, 'task': 'review', 'status': 'completed',
                               'started_at': f'2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00',
                               'artifacts': [{'payload': 'x' * 16384}]}, path)
            os.utime(path, ns=(1700000000000000000, 1700000000000000000))

    def test_repeated_small_listing_reads_only_requested_complete_records(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root)
            expected = history.list_runs(root, limit=7)
            original = history.safe_load_json
            reads = []

            def load(path, *args, **kwargs):
                reads.append(Path(path).name)
                return original(path, *args, **kwargs)

            with patch.object(history, 'safe_load_json', side_effect=load):
                actual = history.list_runs(root, limit=7)
            self.assertEqual(expected, actual)
            self.assertEqual([f'run_{i:04d}' for i in range(1, 8)], [row['id'] for row in actual])
            self.assertEqual(7, len(reads))
            actual[0]['artifacts'][0]['payload'] = 'caller edit'
            self.assertEqual('x' * 16384, history.list_runs(root, limit=1)[0]['artifacts'][0]['payload'])

    def test_replacement_in_place_edit_new_file_and_deletion_refresh_the_order(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=10)
            history.list_runs(root, limit=3)
            path = Path(root, 'run_0010.json')
            row = json.loads(path.read_text())
            row['started_at'] = '2027-01-01T00:00:00+00:00'
            atomic_json_write(row, path)
            self.assertEqual(row['id'], history.list_runs(root, limit=1)[0]['id'])
            before = path.stat()
            raw = path.read_bytes().replace(b'2027-01-01', b'2025-01-01')
            self.assertEqual(before.st_size, len(raw))
            path.write_bytes(raw)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
            fresh = {'id': 'run_new', 'task': 'audio', 'status': 'running',
                     'started_at': '2028-01-01T00:00:00+00:00'}
            atomic_json_write(fresh, Path(root, 'run_new.json'))
            self.assertEqual(fresh, history.list_runs(root, limit=1)[0])
            Path(root, 'run_new.json').unlink()
            self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])

    def test_selected_summary_updates_and_corruption_are_not_hidden_by_cache(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=10)
            history.list_runs(root, limit=3)
            history.update_run(root, 'run_0001', {'next_action': 'Review output'})
            self.assertEqual('Review output', history.list_runs(root, limit=1)[0]['next_action'])
            path = Path(root, 'run_0001.json')
            saved = path.read_bytes()
            path.write_text('{broken')
            self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])
            path.write_bytes(saved)
            self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])

    def test_cache_isolates_history_directories_and_retains_the_response_cap(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.make_history(first, count=510)
            self.make_history(second, count=3)
            self.assertEqual(500, len(history.list_runs(first, limit=1000)))
            self.assertEqual(3, len(history.list_runs(second, limit=1000)))
            self.assertEqual(2, len(history.list_runs(first, limit=2)))

    def test_complete_recovery_scan_primes_the_first_api_listing(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root)
            self.assertEqual([], history.mark_interrupted_runs(root))
            original = history.safe_load_json
            reads = []

            def load(path, *args, **kwargs):
                reads.append(Path(path).name)
                return original(path, *args, **kwargs)

            with patch.object(history, 'safe_load_json', side_effect=load):
                actual = history.list_runs(root, limit=7)
            self.assertEqual(7, len(actual))
            self.assertEqual(7, len(reads))
