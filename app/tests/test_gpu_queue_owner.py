"""Native queue owner launch, exact lease admission and initiating-parent loss."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_gpu_owner_death as wrapper_tests
from tests.test_gpu_job import GPU_JOB, isolated_env

OWNER = Path(GPU_JOB).parent / "app/gpu_queue_owner.py"


class QueueOwnerTests(unittest.TestCase):
    setUp = wrapper_tests.GpuOwnerDeathTests.setUp

    def check_parent_death(self, sig):
        launcher = self.root / "launcher.sh"
        owner_pid_file = self.root / "owner.pid"
        launcher.write_text("#!/bin/bash\nname=$1; shift\nexec 9>\"$GPU_LOCK\"\nflock 9\n\"$GPU_OWNER_PYTHON\" \"$GPU_OWNER_ENTRY\" \"$$\" \"$GPU_WRAPPER_SCRIPT\" \"$name\" \"$@\" &\nowner=$!\nprintf '%s' \"$owner\" > \"$GPU_OWNER_PID_FILE\"\nwait \"$owner\"\n")
        launcher.chmod(0o755)
        with patch.dict(os.environ, {"GPU_OWNER_PYTHON": sys.executable, "GPU_OWNER_ENTRY": str(OWNER),
                                    "GPU_WRAPPER_SCRIPT": GPU_JOB, "GPU_OWNER_PID_FILE": str(owner_pid_file)}),              patch.object(wrapper_tests, "GPU_JOB", str(launcher)):
            wrapper_tests.GpuOwnerDeathTests.check_death(self, sig, check_records=False)
        owner_pid = int(owner_pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                reaped, _ = os.waitpid(owner_pid, os.WNOHANG)
            except ChildProcessError:
                return
            if reaped:
                return
            time.sleep(.01)
        self.fail("queue owner did not exit after reaping descendants")

    def test_hup_survivor_owns_lease_until_descendants_exit(self):
        self.check_parent_death(signal.SIGHUP)

    def test_sigkill_survivor_owns_lease_until_descendants_exit(self):
        self.check_parent_death(signal.SIGKILL)

    def test_open_but_unacquired_lease_is_refused_before_worker(self):
        marker = self.root / "worker-ran"
        launcher = self.root / "unheld.sh"
        launcher.write_text("#!/bin/bash\nexec 9>\"$GPU_LOCK\"\n\"$GPU_OWNER_PYTHON\" \"$GPU_OWNER_ENTRY\" \"$$\" \"$GPU_WRAPPER_SCRIPT\" unheld touch \"$WORKER_MARKER\"\n")
        env = isolated_env(str(self.root), GPU_OWNER_PYTHON=sys.executable,
                           GPU_OWNER_ENTRY=str(OWNER), GPU_WRAPPER_SCRIPT=GPU_JOB, WORKER_MARKER=str(marker))
        result = subprocess.run(["bash", str(launcher)], env=env, capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("no acquired exclusive flock", result.stderr)

    def test_unrelated_parent_claim_is_refused_before_worker(self):
        marker = self.root / "unrelated-parent-worker"
        result = subprocess.run([sys.executable, str(OWNER), "1", GPU_JOB,
                                 "wrong-parent", "touch", str(marker)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("before owner admission", result.stderr)

    def test_missing_interpreter_refuses_wrapper_before_worker_admission(self):
        marker = self.root / "missing-python-worker"
        env = isolated_env(str(self.root), ALLOW_DIRTY_TREE="1", GPU_NOTIFY="0",
                           GPU_OWNER_PYTHON=str(self.root / "absent-python"))
        result = subprocess.run(["bash", GPU_JOB, "missing-owner", "touch", str(marker)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertFalse(marker.exists())
        text = Path(env["GPU_QLOG"]).read_text()
        self.assertNotIn("START", text)
        self.assertIn("GPU owner unavailable", text)
        self.assertEqual([], list(Path(env["GPU_PENDING_DIR"]).iterdir()))
