"""Import and dispatch boundaries when POSIX-only signals are unavailable."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil


class SubprocessOwnerPortabilityTests(unittest.TestCase):
    def test_standalone_deployment_runs_owned_child_without_optional_identity_helper(self):
        app_dir=Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('subprocess_ownership.py','windows_subprocess_owner.py','windows_subprocess_job.py',
                         'macos_subprocess_owner.py','macos_subprocess_kernel.py'):
                shutil.copy2(app_dir/name,Path(tmp)/name)
            script=r"""
import subprocess,sys
sys.path.insert(0,sys.argv[1])
import subprocess_ownership as owned
assert owned._LOADED_SPEECH_VERIFICATION_CODE_SHA256 is None
result=owned.run_owned_capture([sys.executable,'-c',"print('owned-child')"],timeout=5)
assert result.returncode == 0,result.stderr
assert result.stdout.strip() == b'owned-child',result.stdout
"""
            result=subprocess.run([sys.executable,'-I','-c',script,tmp],capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_missing_sigkill_does_not_break_non_linux_worker_admission(self):
        app_dir = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            runner = r"""
import signal
import subprocess
import sys
from types import SimpleNamespace
# Windows Python does not export this POSIX signal. Remove it before import,
# rather than merely mocking the platform after function defaults are evaluated.
if hasattr(signal, 'SIGKILL'):
    del signal.SIGKILL
import subprocess_ownership as ownership
ownership.sys = SimpleNamespace(platform=sys.argv[1], executable=sys.executable)
if sys.argv[1] == 'win32':
    # Native Win32 APIs are unavailable in this isolated Linux interpreter.
    # Exercise the Windows dispatch/import boundary, with the native launcher
    # explicitly replaced by a CPU Popen shim (containment tested separately).
    import windows_subprocess_owner
    def start_windows_shim(command, *, exit_notice_path=None, termination_grace=None, **kwargs):
        assert exit_notice_path is None and termination_grace is None
        return subprocess.Popen(command, **kwargs)
    windows_subprocess_owner.start_windows_owned_subprocess = start_windows_shim
worker = ownership.start_owned_subprocess(
    [sys.executable, '-c', 'import os;print(os.environ["OWNER_FIXTURE"]);print(os.getcwd())'],
    gpu_lease_fd=12345, task_lease_fd=12346, stop_receipt_path='unused',
    cwd=sys.argv[2], env={**__import__('os').environ, 'OWNER_FIXTURE':'worker-started'},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
stdout, stderr = worker.communicate(timeout=5)
assert worker.returncode == 0, (worker.returncode, stderr)
assert stdout.splitlines() == ['worker-started', sys.argv[2]], stdout
assert not hasattr(worker, '_alexandria_control')
"""
            for platform in ('win32', 'freebsd'):
                with self.subTest(platform=platform):
                    result = subprocess.run(
                        [sys.executable, '-c', runner, platform, tmp],
                        env={**os.environ, 'PYTHONPATH': str(app_dir)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, result.returncode, result.stderr)
