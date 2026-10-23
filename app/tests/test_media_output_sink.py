"""Real child lifetime and cumulative output bounds for streaming media sinks."""
import subprocess
import socket
import sys
import threading
import unittest
from unittest.mock import patch

import export_quality as quality
from tts import ExportCancelled


class MediaOutputSinkTests(unittest.TestCase):
    def run_check(self, code, sink, *, limit=131072, timeout=5, cancel=None):
        processes = []
        start = quality.start_owned_subprocess
        def capture(*args, **kwargs):
            process = start(*args, **kwargs)
            processes.append(process)
            return process
        try:
            with patch.object(quality, 'start_owned_subprocess', side_effect=capture):
                return quality._run_owned_check([sys.executable, '-c', code],
                    cancel, timeout, output_limit=limit, consume_output=sink)
        finally:
            self.assertEqual(1, len(processes))
            self.assertIsNotNone(processes[0].poll(), 'Owned child survived sink exit')
            self.assertTrue(processes[0].stdout.closed)
            self.assertTrue(processes[0].stderr.closed)

    def test_success_streams_bounded_pieces_without_retaining_output(self):
        blocks = []
        result = self.run_check('import sys;sys.stdout.buffer.write(b"x"*100001)', blocks.append)
        self.assertEqual(b'', result)
        self.assertEqual(b'x' * 100001, b''.join(blocks))
        self.assertLessEqual(max(map(len, blocks)), 65536)

    def test_limit_is_cumulative_even_when_sink_does_not_retain_output(self):
        blocks = []
        with self.assertRaisesRegex(ValueError, 'allowance'):
            self.run_check('import sys;sys.stdout.buffer.write(b"x"*100001)', blocks.append, limit=65536)
        self.assertLessEqual(sum(map(len, blocks)), 65536)

    def test_sink_failure_stops_child_and_releases_pipes(self):
        def fail(block):
            raise OSError('Synthetic full disk')
        with self.assertRaisesRegex(ValueError, 'Synthetic full disk'):
            self.run_check('import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)', fail)

    def test_timeout_stops_owned_child_without_sink_output(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_check('import time;time.sleep(30)', lambda block: self.fail('Unexpected data'), timeout=.2)

    def test_cancellation_after_output_waits_for_sink_then_stops_child(self):
        cancelled = threading.Event()
        blocks = []
        def consume(block):
            blocks.append(block)
            cancelled.set()
        with self.assertRaises(ExportCancelled):
            self.run_check('import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)',
                           consume, cancel=cancelled.is_set)
        self.assertEqual(65536, sum(map(len, blocks)))

    def test_reader_cancellation_retains_its_type_after_child_cleanup(self):
        cancelled = ExportCancelled('Cancelled inside the audio sink')
        def consume(block):
            raise cancelled
        with self.assertRaises(ExportCancelled) as result:
            self.run_check('import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)',
                           consume)
        self.assertIs(cancelled, result.exception)

    def test_cancellation_reaps_grandchild_holding_output_after_parent_exit(self):
        grandchild = ('import socket,sys,time;'
            's=socket.socket();s.bind(("127.0.0.1",0));s.listen();'
            'data=(str(s.getsockname()[1])+"\\n").encode();'
            'sys.stdout.buffer.write(data+b"x"*(65536-len(data)));sys.stdout.flush();'
            'time.sleep(60)')
        parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(grandchild) + '])'
        ports = []
        def consume(block):
            ports.append(int(block.split(b'\n', 1)[0]))
            raise ExportCancelled('Cancel descendant holding output')
        with self.assertRaises(ExportCancelled):
            self.run_check(parent, consume)
        self.assertEqual(1, len(ports))
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(0, probe.connect_ex(('127.0.0.1', ports[0])),
                                'Owned grandchild still holds its listener')
