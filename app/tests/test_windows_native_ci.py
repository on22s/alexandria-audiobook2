"""Native checks remain explicit, complete and required on the correct runner."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'app/tests/native_windows_owner_checks.py'


class NativeWindowsCiTests(unittest.TestCase):
    def test_ci_runs_all_four_native_methods_on_windows(self):
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnerTests')
        methods = {node.name for node in cls.body if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        self.assertEqual({'test_inherited_pipe_retains_detached_child_ownership',
                          'test_closed_pipe_retains_detached_child_ownership',
                          'test_native_high_bit_worker_exit_status_is_preserved',
                          'test_parent_exit_closes_the_only_job_handle_and_stops_its_members'}, methods)
        self.assertIn('result.testsRun == 4 and not result.skipped', source)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text().split('  windows-owner:', 1)[1]
        self.assertIn('runs-on: windows-latest', workflow)
        self.assertIn('run: python tests/native_windows_owner_checks.py', workflow)
        self.assertIn('PYTHONPATH: .', workflow)
        self.assertFalse((SCRIPT.parent / 'test_windows_owner_native.py').exists())

    def test_wrong_host_is_refused_instead_of_printing_a_successful_skipped_suite(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ, 'PYTHONPATH': str(ROOT / 'app')},
                                capture_output=True, text=True, timeout=10)
        if sys.platform == 'win32':
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('Ran 4 tests', result.stderr)
            self.assertIn('OK', result.stderr)
        else:
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('nothing was verified', result.stderr)
            self.assertNotIn('OK', result.stderr)
