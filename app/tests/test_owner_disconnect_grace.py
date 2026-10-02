"""Kernel lease survives graceful disconnect until stubborn descendants reap."""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_subprocess_finally import is_live


class DisconnectGraceTests(unittest.TestCase):
    def check_disconnect(self, grace):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease_path = root / "gpu.lock"
            pids_path = root / "pids.json"
            child_ready = root / "child-ready"
            child_term = root / "child-term"
            parent_term = root / "parent-term"
            child = "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM,lambda *_: pathlib.Path(sys.argv[2]).write_text('term')); pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
            worker = "import json,os,pathlib,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,lambda *_: pathlib.Path(sys.argv[5]).write_text('term')); p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3],sys.argv[4]]); pathlib.Path(sys.argv[2]).write_text(json.dumps([os.getpid(),p.pid])); p.wait()"
            runner = "import os,socket,sys; from subprocess_ownership import run_owned_command; os.fstat(int(sys.argv[2])); channel=socket.socket(fileno=int(sys.argv[1])); options={} if sys.argv[3]=='default' else {'disconnect_signal':15,'stop_timeout':20}; result=run_owned_command(channel,sys.argv[4:],**options); sys.exit(128-result if result<0 else result)"
            lock = open(lease_path, "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
            control, peer = socket.socketpair()
            control.settimeout(5)
            owner = subprocess.Popen([sys.executable, "-c", runner, str(peer.fileno()), str(lock.fileno()),
                                      "grace" if grace else "default", sys.executable, "-c", worker,
                                      child, str(pids_path), str(child_ready), str(child_term), str(parent_term)],
                                     pass_fds=(peer.fileno(), lock.fileno()), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            peer.close()
            lock.close()
            pids = []
            try:
                response = control.recv(4096)
                self.assertIn(b"started", response, "owner did not admit its actual worker: " +
                              (owner.stderr.read().decode() if not response else repr(response)))
                deadline = time.monotonic() + 5
                while not child_ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(child_ready.exists())
                pids = json.loads(pids_path.read_text())
                if grace:
                    for pid in pids:
                        os.kill(pid, signal.SIGSTOP)
                started = time.monotonic()
                control.close()
                saw_term = False
                with open(lease_path, "a") as probe:
                    while owner.poll() is None:
                        elapsed = time.monotonic() - started
                        self.assertLess(elapsed, 25, "owner failed to finish stopping its tree")
                        try:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            free = True
                            fcntl.flock(probe, fcntl.LOCK_UN)
                        except BlockingIOError:
                            free = False
                        self.assertFalse(free and any(is_live(pid) for pid in pids),
                                         "GPU lease released over live descendants")
                        if grace and elapsed < 19:
                            self.assertTrue(all(is_live(pid) for pid in pids), "checkpoint grace shortened")
                            saw_term |= child_term.exists() and parent_term.exists()
                        time.sleep(.03)
                    owner.wait(timeout=2)
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                elapsed = time.monotonic() - started
                if grace:
                    self.assertGreaterEqual(elapsed, 19.9)
                    self.assertTrue(saw_term, "paused workers must receive CONT and TERM before KILL")
                else:
                    self.assertLess(elapsed, 2, "UI disconnect policy unexpectedly gained queue grace")
                self.assertFalse(any(is_live(pid) for pid in pids))
                self.assertEqual(137, owner.returncode, owner.stderr.read().decode())
            finally:
                control.close()
                if owner.poll() is None:
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    owner.wait(timeout=5)
                owner.stderr.close()

    def test_queue_disconnect_retains_full_checkpoint_grace_and_lease(self):
        self.check_disconnect(True)

    def test_ui_disconnect_keeps_existing_immediate_cleanup(self):
        self.check_disconnect(False)
