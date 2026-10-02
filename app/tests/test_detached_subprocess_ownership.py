"""Native ownership invariants for descendants that escape the worker group."""
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
from tests.test_subprocess_finally import is_live


@unittest.skipUnless(sys.platform == "linux", "Native Linux descendant ownership")
class DetachedOwnershipTests(unittest.TestCase):
    def check_descendant(self, retain_pipe):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            release = root / "release"
            ready = root / "ready"
            state = {"running": False, "logs": [], "process": None,
                     "pid": None, "cancel": False, "paused": False}
            child_code = """import os,pathlib,sys,time
release,ready=map(pathlib.Path,sys.argv[1:])
ready.write_text('ready')
while not release.exists():time.sleep(.005)
os.close(1);os.close(2)
time.sleep(30)
"""
            wrapper_code = """import pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3],sys.argv[4]],start_new_session=True)
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
while not pathlib.Path(sys.argv[4]).exists():time.sleep(.005)
"""
            if not retain_pipe:
                release.write_text("close before parent exit")
            real_queue = queue.Queue
            class FastPollQueue(real_queue):
                def get(self, block=True, timeout=None):
                    return super().get(block=block, timeout=.001 if timeout is not None else None)
            def warning(message, *args, **kwargs):
                if "assuming reader thread died" in str(message):
                    release.write_text("close after inherited-pipe timeout")
            outcome = []
            def run():
                try:
                    outcome.append(core.run_process(
                        [sys.executable, "-c", wrapper_code, child_code,
                         str(pid_path), str(release), str(ready)], "_test_detached", tmp))
                except BaseException as error:
                    outcome.append(error)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
            worker = threading.Thread(target=run)
            pid = None
            try:
                with patch.dict(core.process_state, {"_test_detached": state}), \
                     patch.object(core, "_init_task_log", return_value=str(root / "run.log")), \
                     patch.object(core.queue, "Queue", FastPollQueue), \
                     patch.object(core.logger, "warning", side_effect=warning):
                    worker.start()
                    deadline = time.monotonic() + 4
                    while not pid_path.exists() and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertTrue(pid_path.exists(), "worker did not start its descendant")
                    pid = int(pid_path.read_text())
                    worker.join(timeout=3)
                    self.assertIsNone(unrelated.poll(), "unrelated process was stopped")
                    self.assertFalse(
                        not state["running"] and is_live(pid),
                        "task slot was released while the new-session descendant was live")
                    state["cancel"] = True
                    core._send_signal_tree(state["process"], signal.SIGTERM)
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive(), "cancel did not release the owned worker")
                    self.assertFalse(is_live(pid), "cancel left the escaped descendant alive")
                    self.assertFalse(state["running"])
                    self.assertIsNone(state["process"])
                    self.assertEqual([0], outcome)
            finally:
                state["cancel"] = True
                release.write_text("fixture teardown")
                if pid is None and pid_path.exists():
                    pid = int(pid_path.read_text())
                if pid is not None and is_live(pid):
                    os.kill(pid, signal.SIGKILL)
                process = state.get("process")
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                worker.join(timeout=5)
                unrelated.terminate()
                unrelated.wait(timeout=3)
                self.assertFalse(worker.is_alive(), "fixture left its worker thread alive")

    def test_inherited_pipe_timeout_keeps_descendant_owned(self):
        self.check_descendant(retain_pipe=True)

    def test_closed_pipe_does_not_release_a_live_descendant(self):
        self.check_descendant(retain_pipe=False)

    def test_pause_resume_and_force_cancel_reach_a_detached_term_refuser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            pulse = root / "pulse"
            state = {"logs": [], "process": None, "pid": None,
                     "cancel": False, "paused": False}
            child_code = """import pathlib,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pulse=pathlib.Path(sys.argv[1]);counter=0
while True:
 counter+=1;pulse.write_text(str(counter));time.sleep(.01)
"""
            wrapper_code = """import pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
while not pathlib.Path(sys.argv[3]).exists():time.sleep(.005)
"""
            outcome = []
            def run():
                try:
                    outcome.append(core._stream_subprocess_to_logs(
                        [sys.executable, "-c", wrapper_code, child_code,
                         str(pid_path), str(pulse)], tmp, state))
                except BaseException as error:
                    outcome.append(error)
            def wait_for(predicate):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.01)
                self.fail("native process did not reach the expected state")
            worker = threading.Thread(target=run)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
            pid = None
            try:
                with patch.object(core, "CANCEL_TERMINATE_GRACE_SECONDS", .3):
                    worker.start()
                    wait_for(lambda: pid_path.exists() and pulse.exists())
                    pid = int(pid_path.read_text())
                    time.sleep(.1)
                    process = state["process"]
                    self.assertIsNotNone(process)
                    self.assertIsNone(process.poll())
                    wait_for(lambda: int(Path(f"/proc/{pid}/stat").read_text().rsplit(')',1)[1].split()[1]) == process.pid)
                    core._send_signal_tree(process, signal.SIGSTOP)
                    wait_for(lambda: Path(f"/proc/{pid}/stat").read_text().rsplit(')',1)[1].split()[0] == 'T')
                    paused_pulse = pulse.read_text()
                    time.sleep(.1)
                    self.assertEqual(paused_pulse, pulse.read_text())
                    self.assertIsNone(process.poll(), "paused supervisor abandoned ownership")
                    core._send_signal_tree(process, signal.SIGCONT)
                    wait_for(lambda: pulse.read_text() != paused_pulse)
                    core._send_signal_tree(process, signal.SIGTERM)
                    time.sleep(.1)
                    self.assertTrue(is_live(pid), "TERM grace was bypassed")
                    self.assertTrue(worker.is_alive())
                    state["cancel"] = True
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive(), "force escalation left owned work live")
                    self.assertFalse(is_live(pid))
                    self.assertEqual([(0, [])], outcome)
                    self.assertIsNone(state["process"])
                    self.assertIsNone(state["pid"])
                    self.assertIsNone(unrelated.poll())
            finally:
                state["cancel"] = True
                process = state.get("process")
                if process is not None:
                    try:
                        core._send_signal_tree(process, signal.SIGKILL)
                    except OSError:
                        pass
                if pid is not None and is_live(pid):
                    os.kill(pid, signal.SIGKILL)
                worker.join(timeout=5)
                unrelated.terminate()
                unrelated.wait(timeout=3)
                self.assertFalse(worker.is_alive())

    def test_internal_owner_failure_reaps_work_before_returning_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "child.pid"
            wrapper = """import pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
"""
            owner = """import pathlib,socket,sys,time
sys.path.insert(0,sys.argv[1])
import subprocess_ownership as ownership
real_select=ownership.select.select
pid_path=pathlib.Path(sys.argv[3])
def fail_control_read(readers,writers,errors,timeout):
 if readers:
  deadline=time.monotonic()+3
  while not pid_path.exists() and time.monotonic()<deadline:time.sleep(.005)
  raise RuntimeError('fixture owner dispatch failure')
 return real_select(readers,writers,errors,timeout)
ownership.select.select=fail_control_read
with socket.socket(fileno=int(sys.argv[2])) as control:
 try:ownership.run_owned_command(control,[sys.executable,'-c',sys.argv[4],str(pid_path)])
 except RuntimeError as error:
  assert str(error)=='fixture owner dispatch failure'
  sys.exit(23)
"""
            control, child_control = socket.socketpair()
            process = subprocess.Popen(
                [sys.executable, "-c", owner, str(Path(core.__file__).parent),
                 str(child_control.fileno()), str(pid_path), wrapper],
                pass_fds=(child_control.fileno(),), start_new_session=True)
            child_control.close()
            sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
            pid = None
            try:
                self.assertEqual(23, process.wait(timeout=5))
                self.assertTrue(pid_path.exists())
                pid = int(pid_path.read_text())
                self.assertFalse(is_live(pid), "owner exception abandoned its escaped child")
                self.assertIsNone(sentinel.poll())
            finally:
                control.close()
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
                if pid is None and pid_path.exists():
                    pid = int(pid_path.read_text())
                if pid is not None and is_live(pid):
                    os.kill(pid, signal.SIGKILL)
                sentinel.terminate()
                sentinel.wait(timeout=3)
