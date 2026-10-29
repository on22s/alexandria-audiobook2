"""Native checks remain explicit, complete and required on the correct runner."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'app/tests/native_windows_owner_checks.py'
TRANSACTION_SCRIPT = ROOT / 'app/tests/native_windows_transaction_checks.py'
TRANSACTION_CLASSES = {'NativeWindowsRecoveryQuotaTests': 11, 'NativeWindowsBookStateTests': 5,
                       'NativeWindowsBookMoveTests': 4, 'NativeWindowsPublicationCancellationTests': 3}


class NativeWindowsCiTests(unittest.TestCase):
    def test_ci_runs_all_native_methods_on_windows(self):
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnerTests')
        methods = {node.name for node in cls.body if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        self.assertEqual({'test_inherited_pipe_retains_detached_child_ownership',
                          'test_closed_pipe_retains_detached_child_ownership',
                          'test_native_high_bit_worker_exit_status_is_preserved',
                          'test_parent_exit_closes_the_only_job_handle_and_stops_its_members'}, methods)
        retirement = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == 'NativeWindowsRetirementTests')
        retirement_methods = {node.name for node in retirement.body
                              if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        self.assertEqual({'test_private_stage_finish_removes_stage_and_slot',
                          'test_hard_exit_recovery_removes_private_slot',
                          'test_live_owner_is_preserved'}, retirement_methods)
        self.assertIn('result.testsRun == 7 and not result.skipped', source)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text().split('  windows-owner:', 1)[1]
        self.assertIn('runs-on: windows-latest', workflow)
        self.assertIn('run: python tests/native_windows_owner_checks.py', workflow)
        self.assertIn('run: python -m unittest tests.test_export_staging -v', workflow)
        self.assertIn('PYTHONPATH: .', workflow)
        self.assertFalse((SCRIPT.parent / 'test_windows_owner_native.py').exists())

    def test_wrong_host_is_refused_instead_of_printing_a_successful_skipped_suite(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ, 'PYTHONPATH': str(ROOT / 'app')},
                                capture_output=True, text=True, timeout=10)
        if sys.platform == 'win32':
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('Ran 7 tests', result.stderr)
            self.assertIn('OK', result.stderr)
        else:
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('nothing was verified', result.stderr)
            self.assertNotIn('OK', result.stderr)

    def test_ci_runs_every_native_transaction_check_without_skips(self):
        source = TRANSACTION_SCRIPT.read_text(encoding='utf-8')
        tree = ast.parse(source)
        counts = {node.name: sum(1 for item in node.body if isinstance(item, ast.FunctionDef)
                                 and item.name.startswith('test_'))
                  for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertEqual(TRANSACTION_CLASSES, counts)
        self.assertIn(f'NATIVE_TEST_COUNT = {sum(TRANSACTION_CLASSES.values())}', source)
        self.assertIn('result.testsRun == NATIVE_TEST_COUNT', source)
        self.assertIn('and not result.skipped', source)
        self.assertNotIn('skipUnless', source)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text().split('  windows-owner:', 1)[1]
        self.assertIn('run: python tests/native_windows_transaction_checks.py', workflow)
        # Discovered modules must not reintroduce Windows-only skips: Linux
        # release shards fail on any skipped test.
        for path in (ROOT / 'app/tests').glob('test_*.py'):
            if path.resolve() == Path(__file__).resolve():
                continue  # This guard's own literals.
            text = path.read_text(encoding='utf-8')
            self.assertNotIn("skipUnless(os.name == 'nt'", text, path.name)
            self.assertNotIn("skipUnless(sys.platform == 'win32'", text, path.name)

    def test_native_transaction_checks_refuse_a_non_windows_host(self):
        # Unlike the owner script, this one imports third-party packages, so
        # keep any existing import path after app/.
        path = os.pathsep.join(filter(None, (str(ROOT / 'app'), os.environ.get('PYTHONPATH'))))
        result = subprocess.run([sys.executable, str(TRANSACTION_SCRIPT)],
                                env={**os.environ, 'PYTHONPATH': path},
                                capture_output=True, text=True, timeout=120)
        if sys.platform == 'win32':
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f'Ran {sum(TRANSACTION_CLASSES.values())} tests', result.stderr)
        else:
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('nothing was verified', result.stderr)
            self.assertNotIn('OK', result.stderr)
