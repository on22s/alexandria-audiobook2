import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import core
import run_history as history
from utils import atomic_json_write


class RunHistoryTransactionTests(unittest.TestCase):
    def overlap(self, root, first, second):
        loaded, release, entered = threading.Event(), threading.Event(), threading.Event()
        original = history.get_run
        owner = []

        def get_run(*args, **kwargs):
            record = original(*args, **kwargs)
            if threading.get_ident() == owner[0] and not loaded.is_set():
                loaded.set()
                if not release.wait(5):
                    raise TimeoutError('first writer not released')
            return record

        def run_first():
            owner.append(threading.get_ident())
            return first()

        def run_second():
            entered.set()
            return second()

        with patch.object(history, 'get_run', side_effect=get_run), \
             concurrent.futures.ThreadPoolExecutor(2) as pool:
            one = pool.submit(run_first)
            self.assertTrue(loaded.wait(5))
            two = pool.submit(run_second)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(concurrent.futures.TimeoutError):
                    two.result(timeout=0.15)
            finally:
                release.set()
            return one.result(timeout=5), two.result(timeout=5)

    def test_summary_update_cannot_overwrite_a_concurrent_finish(self):
        with tempfile.TemporaryDirectory() as root:
            run_id = history.start_run(root, 'review')
            self.overlap(root, lambda: history.update_run(root, run_id, {'progress': 3}),
                         lambda: history.finish_run(root, run_id, 'completed'))
            saved = history.get_run(root, run_id)
            self.assertEqual('completed', saved['status'])
            self.assertEqual(3, saved['progress'])
            self.assertIsNotNone(saved['finished_at'])

    def test_concurrent_artifacts_and_finish_keep_both_hashes_and_terminal_status(self):
        with tempfile.TemporaryDirectory() as root:
            history_dir = str(Path(root, 'history'))
            run_id = history.start_run(history_dir, 'review')
            paths = [Path(root, 'one.txt'), Path(root, 'two.txt')]
            paths[0].write_bytes(b'one artifact'); paths[1].write_bytes(b'two artifact')
            self.overlap(root,
                         lambda: history.record_artifact(history_dir, run_id, paths[0], 'report', root),
                         lambda: history.record_artifact(history_dir, run_id, paths[1], 'report', root))
            history.finish_run(history_dir, run_id, 'completed')
            saved = history.get_run(history_dir, run_id)
            self.assertEqual({'one.txt', 'two.txt'}, {row['path'] for row in saved['artifacts']})
            self.assertTrue(all(len(row['sha256']) == 64 for row in saved['artifacts']))
            self.assertEqual('completed', saved['status'])

    def test_startup_recovery_includes_records_older_than_the_ui_limit(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(550):
                run_id = f'run_{index:04d}'
                atomic_json_write({'id': run_id, 'task': 'review', 'status': 'running' if index == 0 else 'completed',
                                   'started_at': f'2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00',
                                   'finished_at': None}, Path(root, run_id + '.json'))
            self.assertEqual(500, len(history.list_runs(root, limit=1000)))
            changed = history.mark_interrupted_runs(root)
            self.assertEqual(['run_0000'], [row['id'] for row in changed])
            self.assertEqual('interrupted', history.get_run(root, 'run_0000')['status'])
            self.assertEqual('completed', history.get_run(root, 'run_0549')['status'])

    def test_shared_runner_balances_normal_failed_and_cancelled_callbacks(self):
        key = '_test_run_history_lifecycle'
        for mode in ('normal', 'early_return', 'failed', 'cancelled'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                state = {'running': True, 'logs': [], 'process': None, 'artifacts': []}
                core.process_state[key] = state

                def callback():
                    if mode == 'failed':
                        raise RuntimeError('fixture failure')
                    if mode == 'cancelled':
                        state['cancel'] = True
                    if mode == 'early_return':
                        return

                try:
                    with patch.object(core, 'RUN_HISTORY_DIR', root), contextlib.redirect_stderr(io.StringIO()):
                        core._run_claimed_background_task(key, callback)
                    records = history.list_runs(root)
                    self.assertEqual(1, len(records))
                    self.assertEqual({'failed': 'failed', 'cancelled': 'cancelled'}.get(mode, 'completed'),
                                     records[0]['status'])
                    self.assertIsNotNone(records[0]['finished_at'])
                    self.assertFalse(state['running'])
                    self.assertNotIn('run_id', state)
                finally:
                    core.process_state.pop(key, None)

    def test_recovery_does_not_overwrite_a_finish_after_its_initial_scan(self):
        with tempfile.TemporaryDirectory() as root:
            run_id = history.start_run(root, 'review')
            original = history.safe_load_json
            finished = []

            def read(path, *args, **kwargs):
                record = original(path, *args, **kwargs)
                if not finished and Path(path).name == run_id + '.json':
                    finished.append(True)
                    history.finish_run(root, run_id, 'completed')
                return record

            with patch.object(history, 'safe_load_json', side_effect=read):
                changed = history.mark_interrupted_runs(root)
            self.assertEqual([], changed)
            self.assertEqual('completed', history.get_run(root, run_id)['status'])

    def test_mutation_respects_a_kernel_lock_held_by_another_process(self):
        import os
        import subprocess
        import sys
        from utils import file_lock
        with tempfile.TemporaryDirectory() as root:
            run_id = history.start_run(root, 'review')
            code = ('import sys; from run_history import update_run; '
                    'print("entered", flush=True); '
                    'update_run(sys.argv[1], sys.argv[2], {"child_progress": 7}); '
                    'print("finished", flush=True)')
            process = None
            try:
                with file_lock(Path(root, run_id + '.json')):
                    process = subprocess.Popen([sys.executable, '-c', code, root, run_id],
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, env={**os.environ,
                                               'PYTHONPATH': str(Path(history.__file__).parent)})
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.2)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(0, process.returncode, stderr)
                self.assertEqual('entered\nfinished\n', stdout)
                self.assertEqual(7, history.get_run(root, run_id)['child_progress'])
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
