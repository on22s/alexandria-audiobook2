"""Native Win32 containment checks, run explicitly by the Windows CI job."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from windows_subprocess_owner import start_windows_owned_subprocess


class NativeWindowsOwnerTests(unittest.TestCase):
    def check_detached_child(self, close_pipe):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path, ready = root / 'child.pid', root / 'child.ready'
            wrapper = """import pathlib,subprocess,sys,time
child_code='import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(60)'
options={'creationflags':subprocess.DETACHED_PROCESS}
if sys.argv[3]=='closed':options.update(stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
else:options.update(stdout=sys.stdout,stderr=sys.stderr)
child=subprocess.Popen([sys.executable,'-c',child_code,sys.argv[2]],**options)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
deadline=time.monotonic()+10
while not pathlib.Path(sys.argv[2]).exists():
 if time.monotonic()>deadline:raise RuntimeError('child did not start')
 time.sleep(.01)
"""
            process = start_windows_owned_subprocess(
                [sys.executable, '-c', wrapper, str(pid_path), str(ready),
                 'closed' if close_pipe else 'inherited'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            control = process._alexandria_windows_owner
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
            try:
                deadline = time.monotonic() + 10
                while (not ready.exists() or control.job.get_active_process_count() != 2):
                    if time.monotonic() >= deadline:
                        self.fail('native wrapper did not exit leaving owner and detached child')
                    time.sleep(.01)
                pid = int(pid_path.read_text())
                self.assertIn(pid, control.job.get_process_ids())
                self.assertIsNone(process.poll(), 'owner exited over detached child')
                time.sleep(.2)
                self.assertIsNone(process.poll())
                self.assertIsNone(sentinel.poll())
                control.terminate()
                process.wait(timeout=10)
                self.assertEqual(0, control.job.get_active_process_count())
                control.close()
                self.assertIsNone(sentinel.poll(), 'job termination reached unrelated process')
                stdout, stderr = process.communicate(timeout=2)
            finally:
                control.close()
                process.wait(timeout=10)
                for stream in (process.stdout, process.stderr):
                    stream.close()
                sentinel.terminate()
                sentinel.wait(timeout=5)

    def test_inherited_pipe_retains_detached_child_ownership(self):
        self.check_detached_child(False)

    def test_closed_pipe_retains_detached_child_ownership(self):
        self.check_detached_child(True)

    def test_native_high_bit_worker_exit_status_is_preserved(self):
        command = 'import ctypes;ctypes.WinDLL("kernel32").ExitProcess(ctypes.c_uint32(0xc0000142))'
        process = start_windows_owned_subprocess([sys.executable, '-c', command])
        try:
            self.assertEqual(0xc0000142, process.wait(timeout=10))
            self.assertEqual(0, process._alexandria_windows_owner.job.get_active_process_count())
        finally:
            process._alexandria_control.close()
            process.wait(timeout=10)

    def test_parent_exit_closes_the_only_job_handle_and_stops_its_members(self):
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready, release = root / 'ready', root / 'release'
            controller = """import json,os,pathlib,sys,time
from windows_subprocess_owner import start_windows_owned_subprocess
process=start_windows_owned_subprocess([sys.executable,'-c','import time;time.sleep(60)'])
control=process._alexandria_windows_owner
pathlib.Path(sys.argv[1]).write_text(json.dumps(control.job.get_process_ids()))
while not pathlib.Path(sys.argv[2]).exists():time.sleep(.01)
os._exit(7)
"""
            process = subprocess.Popen([sys.executable, '-c', controller, str(ready), str(release)],
                                       env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent.parent)})
            handles = []
            try:
                deadline = time.monotonic() + 10
                while not ready.exists():
                    if time.monotonic() > deadline:
                        self.fail('controller did not admit its worker')
                    time.sleep(.01)
                import json
                members = json.loads(ready.read_text())
                self.assertEqual(2, len(members), 'controller must own supervisor and worker')
                for pid in members:
                    handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                    self.assertTrue(handle, ctypes.get_last_error())
                    handles.append(handle)
                    self.assertEqual(0x102, api.WaitForSingleObject(handle, 0))  # WAIT_TIMEOUT
                release.touch()
                self.assertEqual(7, process.wait(timeout=10))
                for handle in handles:
                    self.assertEqual(0, api.WaitForSingleObject(handle, 10000))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                for handle in handles:
                    api.CloseHandle(handle)


def main():
    if sys.platform != 'win32':
        print('Native Windows Job Object checks require Windows; nothing was verified.', file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeWindowsOwnerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 4 and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
