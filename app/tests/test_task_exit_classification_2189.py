"""Synthetic reproductions for #2189 signal/exit classification."""
import os
import signal
import tempfile
import unittest
from unittest.mock import patch

import core


class SignalExitClassificationTests(unittest.TestCase):
    def _run_worker(self, return_code, cancel=False):
        state = {'running': True, 'logs': [], 'status': 'running', 'cancel': cancel}
        handle, log_path = tempfile.mkstemp(prefix='aa2-task-log-', suffix='.log')
        os.close(handle)
        try:
            with patch.dict(core.process_state, {'synthetic_review': state}, clear=False), \
                    patch.object(core, '_init_task_log', return_value=log_path), \
                    patch.object(core, '_stream_subprocess_to_logs',
                                 return_value=(return_code, '')):
                self.assertEqual(return_code, core.run_process(['synthetic'], 'synthetic_review'))
        finally:
            os.unlink(log_path)
        return state

    def test_known_signal_without_app_cancel_is_failed_not_cancelled(self):
        state = self._run_worker(-int(signal.SIGTERM))
        self.assertEqual('failed', state['status'])
        self.assertTrue(any('was killed by SIGTERM' in line for line in state['logs']), state['logs'])
        self.assertFalse(any('was cancelled' in line for line in state['logs']), state['logs'])

    def test_unmapped_signal_keeps_its_number_and_fails(self):
        state = self._run_worker(-9999)
        self.assertEqual('failed', state['status'])
        self.assertTrue(any('signal 9999' in line for line in state['logs']), state['logs'])
        self.assertFalse(any(line.startswith('Error:') for line in state['logs']), state['logs'])

    def test_positive_failure_still_reports_return_code(self):
        state = self._run_worker(1)
        self.assertEqual('failed', state['status'])
        self.assertTrue(any('failed with return code 1' in line for line in state['logs']), state['logs'])

    def test_app_requested_cancel_is_still_cancelled(self):
        state = self._run_worker(-15, cancel=True)
        self.assertEqual('cancelled', state['status'])
        self.assertTrue(any('Task synthetic_review cancelled.' in line for line in state['logs']), state['logs'])


if __name__ == '__main__':
    unittest.main()
