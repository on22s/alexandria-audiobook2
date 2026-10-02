"""Actual CPU output preserves bounded shared logs and complete run artifacts."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import core


class ObservedLogs(list):
    def __init__(self, values=(), cap=20000):
        super().__init__(values)
        self.cap = cap
        self.pops = 0
        self.publications = 0

    def pop(self, index=-1):
        self.pops += 1
        return super().pop(index)

    def extend(self, values):
        super().extend(values)
        self.publications += 1
        if len(self) > self.cap:
            raise AssertionError('Published log exceeds its existing limit')


class SubprocessLogBatchTests(unittest.TestCase):
    def test_queued_burst_preserves_full_artifact_and_capped_shared_tail(self):
        # Read the real child synchronously to produce a known queued burst.
        # This isolates the operation count from thread scheduling variability.
        class FilledReader:
            def __init__(self, target, args, **kwargs):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
            def join(self, **kwargs):
                pass
            def is_alive(self):
                return False
        total, cap = 4096, 100
        logs = ObservedLogs(['previous-run'], cap=cap)
        state = {'logs': logs, 'cancel': False, 'paused': False}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run.log'
            script = 'for i in range(4096): print("line",i)'
            with patch.object(core.threading, 'Thread', FilledReader):
                rc, lines = core._stream_subprocess_to_logs(
                    [sys.executable, '-c', script], tmp, state,
                    max_logs=cap, log_prefix='fixture: ', log_file=str(path))
            expected = [f'fixture: line {i}' for i in range(total)]
            self.assertEqual(0, rc)
            self.assertEqual(expected, lines)
            self.assertEqual(expected, path.read_text().splitlines())
            self.assertIs(logs, state['logs'])
            self.assertEqual(expected[-cap:], logs)
            self.assertEqual(0, logs.pops)
            self.assertLessEqual(logs.publications, total // 128 + 1)

    def test_control_message_appended_during_publication_is_preserved(self):
        class ConcurrentLogs(list):
            injected = False
            def inject(self):
                if not self.injected:
                    super().append('concurrent control message')
                    self.injected = True
            def __setitem__(self, key, value):
                self.inject()
                return super().__setitem__(key, value)
            def extend(self, values):
                self.inject()
                return super().extend(values)
        logs = ConcurrentLogs(['prior'])
        state = {'logs': logs, 'cancel': False}
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines = core._stream_subprocess_to_logs(
                [sys.executable, '-c', "print('child output')"], tmp, state, max_logs=10)
        self.assertEqual(0, rc)
        self.assertEqual(['child output'], lines)
        self.assertEqual(['prior', 'concurrent control message', 'child output'], logs)
        self.assertIs(logs, state['logs'])

    def test_disk_write_failure_warning_stays_ordered_and_bounded(self):
        from unittest.mock import Mock
        handle = Mock()
        handle.write.side_effect = [None, OSError('fixture disk full')]
        logs = ObservedLogs(['previous-run'], cap=4)
        state = {'logs': logs, 'cancel': False}
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'open', return_value=handle, create=True):
            rc, lines = core._stream_subprocess_to_logs(
                [sys.executable, '-c', "print('first');print('second');print('third');print('last')"],
                tmp, state, max_logs=4, log_file=str(Path(tmp) / 'failed.log'))
        self.assertEqual(0, rc)
        self.assertEqual(['first', 'second', 'third', 'last'], lines)
        self.assertEqual('second', logs[0])
        self.assertIn('WARNING: Log file write failed', logs[1])
        self.assertEqual(['third', 'last'], logs[2:])
        self.assertEqual(2, handle.write.call_count)
        self.assertTrue(handle.close.called)
        self.assertIs(logs, state['logs'])

    def test_slow_output_is_published_while_child_waits_without_eof(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / 'release'
            logs = ObservedLogs(['previous-run'], cap=2)
            state = {'logs': logs, 'cancel': False, 'paused': False}
            result, errors = [], []
            script = "import pathlib,sys,time;print('live',flush=True);p=pathlib.Path(sys.argv[1]);\nwhile not p.exists():time.sleep(.01)\nprint('last',flush=True)"
            def run():
                try:
                    result.append(core._stream_subprocess_to_logs(
                        [sys.executable, '-u', '-c', script, str(release)], tmp, state, max_logs=2))
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while 'live' not in logs and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(['previous-run', 'live'], logs)
                self.assertTrue(worker.is_alive())
            finally:
                release.touch()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([(0, ['live', 'last'])], result)
            self.assertIs(logs, state['logs'])
            self.assertEqual(['live', 'last'], logs)
