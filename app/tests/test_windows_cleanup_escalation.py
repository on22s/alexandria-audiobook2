"""Cross-platform policy and failure routing for qualification cleanup."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import verify_release as verifier
import subprocess_ownership as ownership


class CleanupForceFailureTests(unittest.TestCase):
    def test_force_failure_propagates_before_wait_on_both_platforms(self):
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform):
                process = Mock()
                with patch.object(ownership.sys, 'platform', platform), \
                     patch.object(ownership.signal, 'SIGKILL', 9, create=True), \
                     patch.object(ownership, 'is_subprocess_tree_running', return_value=False), \
                     patch.object(ownership, 'terminate_windows_process_tree', side_effect=OSError('kernel force failed')), \
                     patch.object(ownership, 'send_subprocess_signal', side_effect=OSError('kernel force failed')):
                    with self.assertRaisesRegex(OSError, 'kernel force failed'):
                        ownership.stop_owned_subprocess(process, force_after_grace=True)
                process.wait.assert_not_called()

    def test_verifier_retains_posix_policy_and_escalates_only_on_windows(self):
        for platform in ('win32', 'linux', 'darwin'):
            with self.subTest(platform=platform):
                process = Mock()
                with patch.object(verifier.sys, 'platform', platform), \
                     patch.object(verifier, 'stop_owned_subprocess') as stop:
                    verifier.stop_process_group(process, timeout=2)
                stop.assert_called_once_with(process, interrupt=False, timeout=2,
                                             force_after_grace=platform == 'win32')

    def test_native_runner_is_explicitly_wired_and_refuses_other_hosts(self):
        root = Path(__file__).resolve().parents[2]
        script = root / 'app/tests/native_windows_cleanup_checks.py'
        workflow = (root / '.github/workflows/tests.yml').read_text(encoding='utf-8')
        self.assertIn('run: python tests/native_windows_cleanup_checks.py', workflow)
        requirements = (root / 'app/requirements.txt').read_text(encoding='utf-8').splitlines()
        requests_pin = next(line for line in requirements if line.startswith('requests=='))
        windows_setup = workflow.split('  windows-owner:', 1)[1].split(
            '      - name: Verify native Windows qualification cleanup', 1)[0]
        self.assertIn(requests_pin, windows_setup)
        probe = "import runpy,sys; sys.platform='darwin'; runpy.run_path(sys.argv[1],run_name='__main__')"
        result = subprocess.run([sys.executable, '-B', '-c', probe, str(script)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('nothing was verified', result.stderr)
        self.assertNotIn('OK', result.stderr)


if __name__ == '__main__':
    unittest.main()
