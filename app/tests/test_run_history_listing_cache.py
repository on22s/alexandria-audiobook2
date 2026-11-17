import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import run_history as history
from utils import atomic_json_write


class RunHistoryListingCacheTests(unittest.TestCase):
    def test_rapid_preserved_mtime_history_edits_return_every_written_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'run_0002.json')
            Path(root, 'run_0001.json').write_text(json.dumps({'id': 'run_0001', 'started_at': '2026-01-01'}))
            path.write_text(json.dumps({'id': 'run_0002', 'started_at': '2025-01-01'}))
            original = path.stat()
            self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
            for iteration in range(500):
                latest = iteration % 2 == 0
                payload = json.dumps({'id': 'run_0002', 'started_at': '2027-01-01' if latest else '2025-01-01'}).encode()
                self.assertEqual(original.st_size, len(payload))
                path.write_bytes(payload)
                os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual('run_0002' if latest else 'run_0001',
                                 history.list_runs(root, limit=1)[0]['id'], f'write {iteration}')

    def test_recent_equal_history_token_revalidates_sorting(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=2)
            path = Path(root, 'run_0002.json')
            token = history.utils.get_file_cache_token(path)
            with patch('utils.get_file_cache_token', return_value=token):
                self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
                path.write_bytes(path.read_bytes().replace(b'2026-01-01', b'2027-01-01'))
                self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])
                with patch('utils.time.time_ns', return_value=time.time_ns() + 3_000_000_000):
                    with patch('utils.read_file_snapshot', wraps=history.utils.read_file_snapshot) as capture:
                        self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])
                        self.assertEqual(2, capture.call_count)
                    with patch('utils.hashlib.sha256', side_effect=AssertionError('redundant hash')):
                        self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])

    def test_history_snapshot_aba_does_not_cache_sort_date_from_other_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=2)
            path = Path(root, 'run_0002.json')
            first = path.read_bytes()
            second = first.replace(b'2026-01-01', b'2027-01-01')
            original = path.stat()
            real_stat, real_fstat = os.stat, os.fstat
            original_load = history.safe_load_json
            changed = []

            def stat(target, *args, **kwargs):
                return original if os.fspath(target) == str(path) else real_stat(target, *args, **kwargs)

            def fstat(descriptor):
                current = real_fstat(descriptor)
                return original if current.st_ino == original.st_ino else current

            def alternate(target, *args, **kwargs):
                if Path(target) != path or changed:
                    return original_load(target, *args, **kwargs)
                changed.append(True)
                path.write_bytes(second)
                record = original_load(target, *args, **kwargs)
                path.write_bytes(first)
                return record

            with patch('os.stat', side_effect=stat), patch('os.fstat', side_effect=fstat), \
                 patch('utils.get_file_cache_token', return_value=None, create=True):
                with patch.object(history, 'safe_load_json', side_effect=alternate):
                    history.list_runs(root, limit=1)
                self.assertEqual(first, path.read_bytes())
                self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])

    def test_warm_history_cache_does_not_hash_unselected_payloads(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('utils.time.time_ns', wraps=time.time_ns) as clock:
            self.make_history(root)
            clock.return_value = time.time_ns() + 3_000_000_000
            expected = history.list_runs(root, limit=7)
            with patch('utils.hashlib.sha256', side_effect=AssertionError('redundant hash')):
                self.assertEqual(expected, history.list_runs(root, limit=7))

    def test_native_query_failure_hashes_edits_and_never_returns_stale_sorting(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=2)
            self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
            path = Path(root, 'run_0002.json')
            original = path.stat()
            saved = path.read_bytes()
            path.write_bytes(saved.replace(b'2026-01-01', b'2027-01-01'))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            with patch('utils.sys.platform', 'win32'), \
                 patch('utils._get_windows_file_cache_token', side_effect=OSError('query failed')):
                self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])
                with patch('utils.read_file_snapshot', side_effect=PermissionError('fixture denied')):
                    self.assertEqual([], history.list_runs(root, limit=1))
            self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])

    def test_failed_snapshot_evicts_metadata_without_publishing_replacement(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('utils.time.time_ns', wraps=time.time_ns) as clock:
            self.make_history(root, count=2)
            clock.return_value = time.time_ns() + 3_000_000_000
            history.list_runs(root, limit=1)
            path = Path(root, 'run_0002.json')
            path.write_bytes(path.read_bytes().replace(b'2026-01-01', b'2027-01-01'))
            with patch('utils.read_file_snapshot', side_effect=OSError('unstable capture')):
                self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
            _, cache = history._get_run_sort_cache(os.path.abspath(root))
            self.assertNotIn(path.name, cache)
            self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])

    def test_sort_cache_checks_content_when_all_stat_fields_are_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_history(root, count=2)
            path = Path(root, 'run_0002.json')
            original = path.stat()
            real_stat = os.stat
            real_fstat = os.fstat

            def stat(target, *args, **kwargs):
                if os.fspath(target) == str(path):
                    return original
                return real_stat(target, *args, **kwargs)

            def fstat(descriptor):
                current = real_fstat(descriptor)
                return original if current.st_ino == original.st_ino else current

            with patch('os.stat', side_effect=stat), patch('os.fstat', side_effect=fstat), \
                 patch('utils.get_file_cache_token', return_value=None):
                self.assertEqual('run_0001', history.list_runs(root, limit=1)[0]['id'])
                saved = path.read_bytes()
                updated = saved.replace(b'2026-01-01', b'2027-01-01')
                self.assertEqual(len(saved), len(updated))
                path.write_bytes(updated)
                self.assertEqual('run_0002', history.list_runs(root, limit=1)[0]['id'])

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
        with tempfile.TemporaryDirectory() as root, \
             patch('utils.time.time_ns', wraps=time.time_ns) as clock:
            self.make_history(root)
            clock.return_value = time.time_ns() + 3_000_000_000
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
        with tempfile.TemporaryDirectory() as root, \
             patch('utils.time.time_ns', wraps=time.time_ns) as clock:
            self.make_history(root)
            clock.return_value = time.time_ns() + 3_000_000_000
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
