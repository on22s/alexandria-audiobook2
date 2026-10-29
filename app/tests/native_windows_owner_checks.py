"""Native Win32 containment checks, run explicitly by the Windows CI job."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import windows_subprocess_owner as owner

from windows_subprocess_owner import start_windows_owned_subprocess
from export_staging import MARKER, create_export_staging, reclaim_export_staging
from windows_export_staging_retirement import DIRECTORY, reclaim_windows_retired_slots


class NativeWindowsOwnerTests(unittest.TestCase):
    def check_detached_child(self, close_pipe):
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path, ready = root / 'child.pid', root / 'child.ready'
            release = root / 'release-wrapper'
            wrapper = """import json,os,pathlib,subprocess,sys,time
child_code='import json,os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),"prefix":sys.prefix}));time.sleep(60)'
options={'creationflags':subprocess.DETACHED_PROCESS}
if sys.argv[3]=='closed':options.update(stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
else:options.update(stdout=sys.stdout,stderr=sys.stderr)
child=subprocess.Popen([sys.executable,'-c',child_code,sys.argv[2]],**options)
pathlib.Path(sys.argv[1]).write_text(json.dumps({'wrapper':os.getpid(),'redirector':child.pid}))
deadline=time.monotonic()+10
while not pathlib.Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('wrapper was not released')
 time.sleep(.01)
"""
            process = start_windows_owned_subprocess(
                [sys.executable, '-c', wrapper, str(pid_path), str(ready),
                 'closed' if close_pipe else 'inherited', str(release)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            control = process._alexandria_windows_owner
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
            handles = {}
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        receipt = json.loads(pid_path.read_text())
                        child = json.loads(ready.read_text())
                        break
                    except (FileNotFoundError, json.JSONDecodeError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail('native wrapper and actual detached interpreter did not report readiness')
                    time.sleep(.01)
                members = control.job.get_process_ids()
                expected = {process.pid, receipt['wrapper'], receipt['redirector'], child['pid']}
                self.assertTrue(expected.issubset(members), (expected, members))
                self.assertEqual(sys.prefix, child['prefix'])
                for pid in members:
                    handle = api.OpenProcess(0x100000, False, pid)
                    self.assertTrue(handle, ctypes.get_last_error())
                    handles[pid] = handle
                    self.assertEqual(0x102, api.WaitForSingleObject(handle, 0))
                release.touch()
                self.assertEqual(0, api.WaitForSingleObject(handles[receipt['wrapper']], 10000),
                                 'wrapper did not exit after release')
                self.assertIn(child['pid'], control.job.get_process_ids())
                self.assertIsNone(process.poll(), 'owner exited over detached child')
                time.sleep(.2)
                self.assertIsNone(process.poll())
                self.assertIsNone(sentinel.poll())
                control.terminate()
                process.wait(timeout=10)
                self.assertEqual(0, control.job.get_active_process_count())
                for handle in handles.values():
                    self.assertEqual(0, api.WaitForSingleObject(handle, 10000))
                control.close()
                self.assertIsNone(sentinel.poll(), 'job termination reached unrelated process')
                stdout, stderr = process.communicate(timeout=2)
            finally:
                control.close()
                process.wait(timeout=10)
                for handle in handles.values():
                    api.CloseHandle(handle)
                for stream in (process.stdout, process.stderr):
                    stream.close()
                sentinel.terminate()
                sentinel.wait(timeout=5)

    def test_inherited_pipe_retains_detached_child_ownership(self):
        self.check_detached_child(False)

    def test_closed_pipe_retains_detached_child_ownership(self):
        self.check_detached_child(True)

    def test_current_python_environment_override_and_explicit_handle_are_preserved(self):
        import msvcrt
        cases = ((worker, prefix, override)
                 for worker, prefix in ((sys.executable, sys.prefix), (sys._base_executable, sys.base_prefix))
                 for override in (None, sys.executable, sys._base_executable,
                                  os.path.join(os.path.dirname(sys.executable), '.', os.path.basename(sys.executable))))
        for worker, prefix, override in cases:
            with self.subTest(worker=worker, executable_override=override), tempfile.TemporaryDirectory() as temporary:
                working = Path(temporary).resolve()
                ready = working / 'worker-ready.json'
                environment = dict(os.environ, AA2_OWNER_ENV='bounded child environment')
                if sys.executable != sys._base_executable:
                    environment['__pyvenv_launcher__'] = sys._base_executable
                original_environment = dict(environment)
                reader, writer = os.pipe()
                process = None
                try:
                    raw_handle = msvcrt.get_osfhandle(writer)
                    os.set_handle_inheritable(raw_handle, True)
                    child = ('import json,msvcrt,os,pathlib,sys; '
                        'fd=msvcrt.open_osfhandle(int(sys.argv[1]),os.O_WRONLY); '
                        'os.write(fd,b"bounded explicit handle payload");os.close(fd); '
                        'pathlib.Path(sys.argv[2]).write_text(json.dumps({"pid":os.getpid(),"prefix":sys.prefix,'
                        '"executable":sys.executable,"cwd":os.getcwd(),"args":sys.argv[3:],'
                        '"marker":os.environ.get("AA2_OWNER_ENV"),'
                        '"launcher_keys":[k for k in os.environ if k.casefold()=="__pyvenv_launcher__"]})); '
                        'print(json.dumps({"stdin":sys.stdin.read()}),flush=True)')
                    options = {'executable':override} if override is not None else {}
                    process = start_windows_owned_subprocess(
                        [worker, '-c', child, str(raw_handle), str(ready), 'argument with spaces', 'Unicode-\u03a9'],
                        inherited_handles=(raw_handle,), env=environment, cwd=str(working),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, **options)
                    os.close(writer)
                    writer = None
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            row = json.loads(ready.read_text())
                            break
                        except (FileNotFoundError, json.JSONDecodeError):
                            if time.monotonic() >= deadline:
                                self.fail('worker did not report its actual environment')
                            time.sleep(.01)
                    self.assertIn(row['pid'], process._alexandria_windows_owner.job.get_process_ids())
                    self.assertEqual(prefix, row['prefix'])
                    self.assertEqual(worker, row['executable'])
                    self.assertEqual([], row['launcher_keys'])
                    self.assertEqual(working, Path(row['cwd']))
                    self.assertEqual(['argument with spaces', 'Unicode-\u03a9'], row['args'])
                    self.assertEqual('bounded child environment', row['marker'])
                    stdout, stderr = process.communicate(input='bounded stdin payload', timeout=10)
                    self.assertEqual({'stdin':'bounded stdin payload'}, json.loads(stdout))
                    self.assertEqual(b'bounded explicit handle payload', os.read(reader, 1024))
                    self.assertEqual('', stderr)
                    self.assertEqual(0, process.returncode)
                    self.assertEqual(original_environment, environment)
                finally:
                    if process is not None:
                        process._alexandria_windows_owner.close()
                        process.wait(timeout=10)
                    if writer is not None:
                        os.close(writer)
                    os.close(reader)

    def test_native_high_bit_worker_exit_status_is_preserved(self):
        command = 'import ctypes;ctypes.WinDLL("kernel32").ExitProcess(ctypes.c_uint32(0xc0000142))'
        # CreateFileW share mode zero prevents subsequent read opens until
        # CloseHandle, exercising an actual Windows receipt-sharing denial.
        api = ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,
            ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        api.CreateFileW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        original = owner.read_windows_owner_message
        launch = owner._popen_owned_python
        denied, spawned = [], []
        def capture_supervisor(*args, **kwargs):
            process = launch(*args, **kwargs)
            spawned.append(process)
            return process
        def read_with_sharing_denial(path):
            if denied or not path.exists():
                return original(path)
            handle = api.CreateFileW(str(path),0x80000000,0,None,3,0x80,None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return original(path)
            except PermissionError:
                deadline = time.monotonic() + 5
                while spawned[0].poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail('fast supervisor did not exit while its valid receipt was locked')
                    time.sleep(.01)
                denied.append(True)
                raise
            finally:
                self.assertTrue(api.CloseHandle(handle))
        with patch.object(owner,'read_windows_owner_message',side_effect=read_with_sharing_denial), \
             patch.object(owner, '_popen_owned_python', side_effect=capture_supervisor):
            process = start_windows_owned_subprocess([sys.executable, '-c', command])
        try:
            self.assertEqual([True],denied)
            self.assertEqual(0xc0000142, spawned[0].poll())
            self.assertEqual(0xc0000142, process.wait(timeout=10))
            self.assertEqual(0, process._alexandria_windows_owner.job.get_active_process_count())
        finally:
            process._alexandria_control.close()
            process.wait(timeout=10)

    def test_inherited_hidden_drive_environment_survives_python_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / 'saved-drive-\u03a9-\U0001f642'
            directory.mkdir()
            runner = r'''import json,subprocess,sys
from windows_subprocess_owner import start_windows_owned_subprocess
child = "import json,sys;from windows_subprocess_owner import _inherited_windows_environment;print(json.dumps({'preserved':_inherited_windows_environment().get('=C:')==sys.argv[1],'prefix':sys.prefix}),flush=True)"
process = start_windows_owned_subprocess([sys.executable,'-c',child,sys.argv[1]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
try:
 stdout,stderr = process.communicate(timeout=5)
 assert process.returncode == 0,stderr
 row=json.loads(stdout)
 assert row == {'preserved':True,'prefix':sys.prefix},row
finally:
 process._alexandria_windows_owner.close()
 process.wait(timeout=5)
'''
            environment = dict(os.environ)
            environment['=C:'] = str(directory)
            result = subprocess.run([sys.executable, '-c', runner, str(directory)],
                env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

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
from export_staging import create_export_staging, reclaim_export_staging
from windows_export_staging_retirement import DIRECTORY, reclaim_windows_retired_slots
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




class NativeWindowsRetirementTests(unittest.TestCase):
    def test_private_stage_finish_removes_stage_and_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            (root / MARKER).write_bytes(b'user project marker collision')
            stage = create_export_staging(root, 'mp3')
            path = stage.path
            self.assertEqual(root / DIRECTORY, path.parent.parent)
            (path / 'payload.bin').write_bytes(b'native payload')
            stage.finish(remove=True)
            self.assertFalse(path.exists())
            self.assertFalse((root / DIRECTORY / stage._registry_slot).exists())
            self.assertEqual(b'user project marker collision', (root / MARKER).read_bytes())

    def test_hard_exit_recovery_removes_private_slot(self):
        script = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
stage = create_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'native hard-exit payload')
print(stage.path.parent, flush=True)
os._exit(76)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            result = subprocess.run(
                [sys.executable, '-c', script, str(Path(__file__).parents[1]), str(root)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])})
            self.assertEqual(76, result.returncode, result.stderr[-2000:])
            slot = Path(result.stdout.strip().splitlines()[-1])
            self.assertTrue((slot / 'stage' / 'payload.bin').is_file())
            reclaimed = reclaim_export_staging([root])
            self.assertTrue(reclaimed['removed'], reclaimed)
            self.assertFalse(slot.exists())

    def test_live_owner_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'book'
            root.mkdir()
            stage = create_export_staging(root, 'mp3')
            try:
                (stage.path / 'payload.bin').write_bytes(b'native live payload')
                reclaimed = reclaim_export_staging([root])
                self.assertFalse(reclaimed['removed'], reclaimed)
                self.assertTrue(reclaimed['preserved'], reclaimed)
                self.assertFalse(reclaimed['errors'], reclaimed)
                self.assertTrue((stage.path / 'payload.bin').is_file())
            finally:
                stage.finish(remove=True)
            self.assertFalse(stage.path.exists())

def main():
    if sys.platform != 'win32':
        print('Native Windows Job Object checks require Windows; nothing was verified.', file=sys.stderr)
        return 2
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(NativeWindowsOwnerTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(NativeWindowsRetirementTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 9 and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
