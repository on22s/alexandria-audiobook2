"""Unguarded startup recovery steps must be skipped, not fatal (#2107).

Lock timeouts, a preserved book-switch placeholder and a failing run-history
reader all used to abort FastAPI startup. Each step must warn, record a bounded
diagnostic, leave state untouched and let the server boot.
"""

import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from book_placeholder import BookSwitchPlaceholderError
from project import ProjectManager
from run_history import start_run

DIAGNOSTIC_STAGE = 'startup_recovery_skipped'


def _failures(record, operation):
    return [call for call in record.call_args_list
            if call.args and call.args[0] == DIAGNOSTIC_STAGE
            and call.kwargs.get('operation') == operation]


class StartupRecoveryGuardTests(unittest.TestCase):
    def _drive(self, root):
        async def start():
            async with app_module.lifespan(app_module.app):
                pass

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(app_module, 'DATA_DIR', root))
            stack.enter_context(patch.object(app_module, 'RUN_HISTORY_DIR', str(Path(root) / 'history')))
            stack.enter_context(patch.object(app_module, 'EVALUATION_REVIEWS_DIR', str(Path(root) / 'reviews')))
            record = stack.enter_context(patch.object(app_module, 'record_failure'))
            asyncio.run(start())
        return record

    @staticmethod
    def _assert_bounded(call, operation):
        assert call.args[0] == DIAGNOSTIC_STAGE
        assert call.kwargs['task'] == 'startup'
        assert call.kwargs['operation'] == operation
        assert set(call.kwargs) <= {'task', 'operation', 'resource', 'level'}

    def test_admission_lock_timeout_skips_all_recovery_and_starts(self):
        @contextlib.contextmanager
        def busy(_data_dir, timeout=10):
            raise TimeoutError('admission lock held')
            yield

        with tempfile.TemporaryDirectory() as root:
            with patch.object(app_module, 'ensure_startup_recovery', busy), \
                 patch.object(app_module, 'reset_stuck_chunks') as reset, \
                 patch.object(app_module.evaluation_reviews, 'prune_sessions') as prune:
                record = self._drive(root)
            reset.assert_not_called()
            prune.assert_not_called()
            found = _failures(record, 'startup_admission_lock')
            self.assertEqual(1, len(found))
            self._assert_bounded(found[0], 'startup_admission_lock')

    def test_book_lock_timeout_skips_book_recovery_and_starts(self):
        @contextlib.contextmanager
        def busy(*_args, **_kwargs):
            raise TimeoutError('book lock held')
            yield

        with tempfile.TemporaryDirectory() as root:
            chunks_before = json.dumps([{'uid': 'u1', 'status': 'generating', 'text': 'live'}])
            (Path(root) / 'chunks.json').write_text(chunks_before, encoding='utf-8')
            with patch.object(app_module, 'ensure_book_state', busy):
                record = self._drive(root)
            self.assertEqual(chunks_before, (Path(root) / 'chunks.json').read_text(encoding='utf-8'))
            found = _failures(record, 'book_recovery')
            self.assertEqual(1, len(found))
            self._assert_bounded(found[0], 'book_recovery')

    def test_placeholder_value_error_preserves_chunks_and_starts(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            manager.save_chunks([{'uid': 'u1', 'status': 'generating', 'text': 'live'}])
            chunks_path = Path(root) / 'chunks.json'
            before = chunks_path.read_bytes()

            with patch.object(app_module, 'reset_stuck_chunks',
                              side_effect=BookSwitchPlaceholderError(
                                  'A book switch is in progress; chunks are not available yet')):
                record = self._drive(root)

            self.assertEqual(before, chunks_path.read_bytes())
            found = _failures(record, 'book_recovery')
            self.assertEqual(1, len(found))
            self._assert_bounded(found[0], 'book_recovery')

    def test_unrelated_value_error_still_propagates(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(app_module, 'reset_stuck_chunks',
                              side_effect=ValueError('unrelated cleanup bug')):
                with self.assertRaisesRegex(ValueError, 'unrelated cleanup bug'):
                    self._drive(root)

    def test_run_history_failure_preserves_records_and_starts(self):
        for error in (TimeoutError('run lock held'), OSError('cannot read run history')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as root:
                history = str(Path(root) / 'history')
                run_id = start_run(history, 'audio')
                record_path = Path(history) / (run_id + '.json')
                before = record_path.read_bytes()

                with patch.object(app_module, 'mark_interrupted_runs', side_effect=error):
                    record = self._drive(root)

                self.assertEqual(before, record_path.read_bytes())
                found = _failures(record, 'run_history_recovery')
                self.assertEqual(1, len(found))
                self._assert_bounded(found[0], 'run_history_recovery')


if __name__ == '__main__':
    unittest.main()
