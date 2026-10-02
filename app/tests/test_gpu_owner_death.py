"""Native wrapper death must not free a lease over live CPU descendants."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_gpu_job import GPU_JOB, isolated_env
from tests.test_subprocess_finally import is_live


class GpuOwnerDeathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, self.libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, self.libc.prctl(36, 1, 0, 0, 0))
        self.addCleanup(self.libc.prctl, 36, previous.value, 0, 0, 0)

    def check_death(self, sig, stubborn=False, check_records=True):
        provider = self.root / "bin"
        provider.mkdir()
        for name in ("rocm-smi", "nvidia-smi"):
            path = provider / name
            path.write_text("#!/bin/sh\nexit 1\n")
            path.chmod(0o755)
        pids_path = self.root / "worker-pids.json"
        ready = self.root / "heartbeat"
        child_code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.write_text('ready'); time.sleep(30)"
        worker_code = "import json,os,pathlib,subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]]); pathlib.Path(sys.argv[2]).write_text(json.dumps([os.getpid(),child.pid])); child.wait()"
        if stubborn:
            child_code = child_code.replace("import pathlib,sys,time;", "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM,lambda *_: pathlib.Path(sys.argv[1]+'.childterm').write_text('term'));")
            worker_code = worker_code.replace("import json,os,pathlib,subprocess,sys,time;", "import json,os,pathlib,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,lambda *_: pathlib.Path(sys.argv[3]+'.parentterm').write_text('term'));")
        env = isolated_env(self.tmp.name, ALLOW_DIRTY_TREE="1", GPU_NOTIFY="0",
                           PATH=str(provider) + os.pathsep + os.environ["PATH"])
        owner = subprocess.Popen(["bash", GPU_JOB, "owner-death", sys.executable, "-c", worker_code,
                                  child_code, str(pids_path), str(ready)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        pids = []
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), "actual worker descendants must be running")
            pids = json.loads(pids_path.read_text())
            pgid = os.getpgid(pids[0])
            self.assertEqual(pids[0], pgid, "fixture worker must own its setsid group")
            self.assertTrue(all(is_live(pid) for pid in pids))
            if stubborn:
                for pid in pids:
                    os.kill(pid, signal.SIGSTOP)
            started = time.monotonic()
            owner.send_signal(sig)
            owner.wait(timeout=26 if stubborn else 5)
            violation = False
            deadline = time.monotonic() + (25 if stubborn else 5)
            with open(env["GPU_LOCK"], "a") as lock:
                while time.monotonic() < deadline:
                    live = any(is_live(pid) for pid in pids)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lease_free = True
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    except BlockingIOError:
                        lease_free = False
                    if stubborn and time.monotonic() - started < 19:
                        self.assertTrue(all(is_live(pid) for pid in pids), "full checkpoint grace was shortened")
                    if live and lease_free:
                        violation = True
                        break
                    if not live and lease_free:
                        break
                    time.sleep(.01)
            self.assertFalse(violation, "wrapper released the kernel GPU lease over live descendants")
            self.assertFalse(any(is_live(pid) for pid in pids), "owned worker tree was orphaned")
            self.assertIsNone(unrelated.poll(), "unrelated CPU sentinel must survive")
            if stubborn:
                self.assertGreaterEqual(time.monotonic() - started, 19.9)
                self.assertTrue(Path(str(ready)+".childterm").exists())
                self.assertTrue(Path(str(ready)+".parentterm").exists())
            if check_records:
                text = Path(env["GPU_QLOG"]).read_text()
                terminal = [line for line in text.splitlines() if line.split()[1] in ("OK", "FAILED", "INTERRUPTED")]
                self.assertEqual(1, len(terminal), text)
                self.assertEqual([], list(Path(env["GPU_PENDING_DIR"]).iterdir()))
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
            if pids:
                try:
                    os.killpg(pids[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for pid in pids:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            reaped, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if reaped:
                            break
                        time.sleep(.01)
            unrelated.kill()
            unrelated.wait(timeout=5)

    def test_hup_keeps_lease_until_worker_tree_stops(self):
        self.check_death(signal.SIGHUP)

    def test_sigkill_keeps_lease_until_worker_tree_stops(self):
        self.check_death(signal.SIGKILL)

    def test_sigkill_preserves_full_grace_for_paused_term_refusing_tree(self):
        self.check_death(signal.SIGKILL, stubborn=True)
