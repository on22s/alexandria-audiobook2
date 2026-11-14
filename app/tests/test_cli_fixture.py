"""Fixture commands exercise native subprocess execution on each host."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_support import write_python_cli_fixture


class CliFixtureTests(unittest.TestCase):
    def test_real_command_preserves_arguments_output_and_nonzero_status(self):
        with tempfile.TemporaryDirectory(prefix='aa2 cli fixture ') as directory:
            command = write_python_cli_fixture(Path(directory) / 'tool with spaces',
                'import json,sys\n'
                'print(json.dumps(sys.argv[1:]))\n'
                'raise SystemExit(7)\n')
            if os.name == 'nt':
                import ctypes
                code_page = ctypes.windll.kernel32.GetConsoleOutputCP()
            result = subprocess.run([str(command), '--flag', 'two words'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(7, result.returncode, result.stderr)
            self.assertEqual(['--flag', 'two words'], json.loads(result.stdout))
            self.assertEqual('', result.stderr)
            if os.name == 'nt':
                self.assertEqual(code_page, ctypes.windll.kernel32.GetConsoleOutputCP())

    def test_real_command_runs_from_non_ascii_directory(self):
        with tempfile.TemporaryDirectory(prefix='aa2 cli ') as directory:
            root = Path(directory) / 'caf\u00e9-\u65e5\u672c\u8a9e'
            root.mkdir()
            command = write_python_cli_fixture(root / 'tool with spaces',
                'import json,sys\n'
                'print(json.dumps(sys.argv[1:]))\n'
                'raise SystemExit(7)\n')
            if os.name == 'nt':
                import ctypes
                code_page = ctypes.windll.kernel32.GetConsoleOutputCP()
            result = subprocess.run([str(command), '--flag', 'two words'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(7, result.returncode, result.stderr)
            self.assertEqual(['--flag', 'two words'], json.loads(result.stdout))
            self.assertEqual('', result.stderr)
            if os.name == 'nt':
                self.assertEqual(code_page, ctypes.windll.kernel32.GetConsoleOutputCP())
