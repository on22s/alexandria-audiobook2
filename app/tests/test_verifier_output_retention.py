"""Discarded release-gate output remains streamed and summary-checked."""
import contextlib
import io
import sys
import tracemalloc
import unittest

import verify_release as verifier


class VerifierOutputRetentionTests(unittest.TestCase):
    def test_report_streams_all_lines_without_retaining_full_output(self):
        class Sink:
            count = 0
            def write(self, value):
                if value.startswith('payload:'):
                    self.count += 1
                return len(value)
            def flush(self):
                pass

        sink = Sink()
        code = "import time;line='payload:'+'x'*4088\nfor _ in range(1600):\n print(line,flush=True);time.sleep(.001)"
        tracemalloc.start()
        try:
            with contextlib.redirect_stdout(sink):
                self.assertIsNone(verifier.run_report_command(
                    'discard fixture', [sys.executable, '-c', code], '.'))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(1600, sink.count)
        self.assertLess(peak, 4_000_000,
                        f'discarded output was retained: traced peak {peak}')

    def test_discarded_unit_gate_still_checks_success_skips_and_floor(self):
        cases = [
            ('Ran 500 tests in 0.1s\nOK\n', None),
            ('Ran 500 tests in 0.1s\nOK (skipped=1)\n', 'skipped'),
            ('Ran 0 tests in 0.1s\nOK\n', 'floor'),
            ('Ran 500 tests in 0.1s\nFAILED\n', 'successful summary'),
        ]
        for output, error in cases:
            command = [sys.executable, '-c', f'print({output!r},end="")']
            with self.subTest(output=output), contextlib.redirect_stdout(io.StringIO()):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        verifier.run_report_command('summary', command, '.',
                                                    reject_unittest_skips=True)
                else:
                    self.assertIsNone(verifier.run_report_command(
                        'summary', command, '.', reject_unittest_skips=True))

    def test_validator_consumes_stream_without_full_read(self):
        verifier.validate_unittest_output(iter(['noise\n', 'Ran 501 tests in 1s\n', 'OK\n']))
        with self.assertRaisesRegex(ValueError, 'skipped'):
            verifier.validate_unittest_output(iter(['Ran 501 tests in 1s\n', 'OK (skipped=2)\n']))

    def test_default_capture_keeps_complete_decoded_output(self):
        with contextlib.redirect_stdout(io.StringIO()) as console:
            output = verifier.run_command('capture', [sys.executable, '-c',
                "import sys;sys.stdout.buffer.write(b'first\\n\\xa1 last\\n')"], '.')
        self.assertEqual('first\n\\xa1 last\n', output)
        self.assertIn(output, console.getvalue())
