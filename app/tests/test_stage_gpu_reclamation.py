"""CPU-only reclamation must wait for the real kernel GPU lease."""
import fcntl
import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from tests.test_gpu_job import REPO, copy_gpu_owner, isolated_env


class StageGpuReclamationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, self.libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, self.libc.prctl(36, 1, 0, 0, 0))
        self.addCleanup(self.libc.prctl, 36, previous.value, 0, 0, 0)
        copy_gpu_owner(str(self.root))
        self.wrapper = self.root / 'gpu_job.sh'
        shutil.copy(Path(REPO) / 'gpu_job.sh', self.wrapper)
        self.stage = self.root / 'run_chains/lib/stage.sh'
        shutil.copy(Path(REPO) / 'run_chains/lib/stage.sh', self.stage)
        provider = self.root / 'bin'
        provider.mkdir()
        for name, body in (
            ('rocm-smi', "printf 'GPU[0] : VRAM Total Memory (B): 8589934592\\nGPU[0] : VRAM Total Used Memory (B): %s\\n' \"${FIXTURE_USED:-0}\""),
            ('nvidia-smi', 'exit 1')):
            path = provider / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        # Verify actual ancestor/inode/flock ownership without host signals.
        (self.root / 'app/llama_server_process.py').write_text('''import os,pathlib,subprocess,time
subprocess.run(['bash',os.environ['FIXTURE_WRAPPER'],'--check-lock-owner',os.environ['ALEXANDRIA_GPU_LOCK_PID']],check=True)
r=pathlib.Path(os.environ['FIXTURE_ROOT'])
(r/'reclaimed').write_text(str(os.getpid()))
(r/'owner').write_text(os.environ['ALEXANDRIA_GPU_LOCK_PID'])
status=pathlib.Path('/proc/'+os.environ['ALEXANDRIA_GPU_LOCK_PID']+'/status').read_text()
(r/'wrapper').write_text(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
while os.environ.get('FIXTURE_BLOCK') == '1' and not (r/'release').exists(): time.sleep(.01)
''')
        self.env = isolated_env(str(self.root), HOME=str(self.root),
            PATH=str(provider) + os.pathsep + os.environ['PATH'],
            GPU_NOTIFY='0', FIXTURE_ROOT=str(self.root), FIXTURE_WRAPPER=str(self.wrapper),
            REQUIRE_VRAM_GB='4')
        self.program = '''set -uo pipefail
source "$1"
STAGE_LOG_DIR="$2/logs"
STAGE_RESULT[prior]=ok
fixture_root="$2"
pgrep() { return 0; }
pkill() { touch "$fixture_root/reclaimed"; }
run_stage work 10s --needs-vram --requires-ok prior -- "$2/gpu_job.sh" work touch "$2/dispatched"
stage_summary fixture
'''

    def launch(self, **environment):
        return subprocess.Popen(['bash', '-c', self.program, 'fixture', str(self.stage), str(self.root)],
            env=dict(self.env, **environment), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_marker(self, name):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / name).exists(), name)

    def test_held_gpu_lease_prevents_reclamation_then_allows_work(self):
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.launch()
            try:
                self.wait_marker('logs/work.log')
                deadline = time.monotonic() + .25
                while time.monotonic() < deadline:
                    self.assertFalse((self.root / 'reclaimed').exists(), 'cleanup ran before queue lease')
                    self.assertFalse((self.root / 'dispatched').exists())
                    time.sleep(.01)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, stdout + stderr + (self.root / 'logs/work.log').read_text())
        self.assertTrue((self.root / 'reclaimed').exists())
        self.assertTrue((self.root / 'dispatched').exists())
        self.assertIn('VRAM reclaimed', (self.root / 'logs/work.log').read_text())

    def test_direct_command_uses_same_queue_and_reclamation_gate(self):
        self.program = self.program.replace('"$2/gpu_job.sh" work touch', 'touch')
        self.test_held_gpu_lease_prevents_reclamation_then_allows_work()

    def test_self_queuing_command_proves_inherited_owner_without_nesting(self):
        child = self.root / 'self-queue.sh'
        child.write_text('''#!/bin/bash
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$FIXTURE_WRAPPER" nested "$0"
fi
bash "$FIXTURE_WRAPPER" --check-lock-owner "$ALEXANDRIA_GPU_LOCK_PID" || exit 4
touch "$FIXTURE_ROOT/dispatched"
''')
        child.chmod(0o755)
        self.program = self.program.replace('"$2/gpu_job.sh" work touch "$2/dispatched"', '"$2/self-queue.sh"')
        self.test_held_gpu_lease_prevents_reclamation_then_allows_work()

    def test_capacity_gate_runs_after_reclamation_and_refuses_low_memory(self):
        process = self.launch(FIXTURE_USED='7516192768', STAGE_VRAM_WAIT='0')
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertTrue((self.root / 'reclaimed').exists())
        self.assertFalse((self.root / 'dispatched').exists())
        self.assertIn('NO_VRAM', Path(self.env['GPU_QLOG']).read_text())
        self.assertIn('rc=7', stdout)

    def test_lease_remains_held_during_supervised_reclamation(self):
        process = self.launch(FIXTURE_BLOCK='1')
        try:
            self.wait_marker('reclaimed')
            with open(self.env['GPU_LOCK'], 'a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse((self.root / 'dispatched').exists())
        finally:
            (self.root / 'release').touch()
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.assertTrue((self.root / 'dispatched').exists())

    def test_unowned_capacity_check_is_refused(self):
        result = subprocess.run(['bash', str(self.wrapper), '--check-vram', 'unowned'],
            env=dict(self.env, ALEXANDRIA_GPU_LOCK_PID=str(os.getpid())),
            capture_output=True, text=True, timeout=5)
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertFalse((self.root / 'reclaimed').exists())
        self.assertFalse(Path(self.env['GPU_QLOG']).exists())

    def test_wrapper_death_during_reclamation_retains_lease_until_cleanup(self):
        process = self.launch(FIXTURE_BLOCK='1')
        owner = None
        try:
            self.wait_marker('wrapper')
            owner = int((self.root / 'owner').read_text())
            wrapper = int((self.root / 'wrapper').read_text())
            os.kill(owner, signal.SIGSTOP)
            os.kill(wrapper, signal.SIGKILL)
            # The actual cleanup worker is still live, so admission must wait.
            with open(self.env['GPU_LOCK'], 'a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse((self.root / 'dispatched').exists())
        finally:
            if owner is not None:
                os.kill(owner, signal.SIGCONT)
            stdout, stderr = process.communicate(timeout=10)
            if owner is not None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pid, _ = os.waitpid(owner, os.WNOHANG)
                    if pid:
                        break
                    time.sleep(.01)
                else:
                    self.fail('reclamation owner did not reap and exit')
        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertFalse((self.root / 'dispatched').exists())
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
