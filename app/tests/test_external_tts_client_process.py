"""Native cached SDK interpreters, blocked constructors, deadlines and shutdown."""
import concurrent.futures
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import external_tts_client as transport


FAKE_SDK = """
from concurrent.futures import ThreadPoolExecutor
import json, os, threading, time, wave
from pathlib import Path
ROOT = Path(__file__).parent
def record(**values):
    with (ROOT/'events.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(pid=os.getpid(), **values))+'\\n')
class Client:
    def __init__(self,url):
        self.url=url;self.count=0;self.executor=ThreadPoolExecutor(2)
        record(event='init',url=url)
        if url.endswith('constructor-hang'):
            def blocked():
                record(event='blocked-constructor');threading.Event().wait()
            self.executor.submit(blocked).result()
    def predict(self,*args,**kwargs):
        self.count+=1;text=kwargs.get('text',args[2] if len(args)>2 else '')
        reference=None
        if args and isinstance(args[0],dict): reference=Path(args[0]['path']).read_bytes().hex()
        record(event='call',count=self.count,text=text,reference=reference,
               reference_path=args[0]['path'] if args and isinstance(args[0],dict) else None)
        def work():
            if text.rstrip('.')=='hang':
                record(event='blocked-predict');threading.Event().wait()
            if text=='wait':
                while not (ROOT/'release').exists(): time.sleep(.01)
            if text=='error': raise RuntimeError('token=private-fixture-secret')
            output=ROOT/('provider-'+str(os.getpid())+'.wav')
            with wave.open(str(output),'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed'))
                wav.writeframes(self.count.to_bytes(2,'little')*2400)
            return (str(output),)
        return self.executor.submit(work).result()
"""


def is_alive(pid):
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ExternalClientProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "gradio_client.py").write_text(FAKE_SDK)
        self.environment = dict(os.environ, PYTHONPATH=str(self.root),
                                TMPDIR=str(self.root), TEMP=str(self.root), TMP=str(self.root))
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.temporary.cleanup()

    def client(self, suffix="ok", timeout=3):
        client = transport.ExternalTTSClient("http://fixture/" + suffix, timeout,
                                             environment=self.environment)
        self.clients.append(client)
        return client

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_stopped(self, pids):
        deadline = time.monotonic() + 3
        while any(is_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(all(not is_alive(pid) for pid in pids), pids)

    def test_cached_client_and_private_output_survive_subsequent_sdk_overwrites(self):
        client = self.client()
        with client.predict(text="first") as first:
            original = Path(first[0]).read_bytes()
            with client.predict(text="second") as second:
                self.assertNotEqual(original, Path(second[0]).read_bytes())
                self.assertEqual(original, Path(first[0]).read_bytes())
            second_directory = Path(second[0]).parent
            self.assertFalse(second_directory.exists())
        self.assertFalse(Path(first[0]).parent.exists())
        events = self.events()
        self.assertEqual(1, sum(row["event"] == "init" for row in events))
        self.assertEqual([1, 2], [row["count"] for row in events if row["event"] == "call"])
        pids = {row["pid"] for row in events}
        self.assertEqual(1, len(pids))
        directory = Path(client._resources.directory.name)
        client.close()
        self.assertFalse(directory.exists())
        self.assert_stopped(pids)

    def test_predict_and_constructor_hangs_are_killed_and_reaped_before_replacement(self):
        # Warm native platform ownership admission before measuring a request.
        warm = self.client()
        with warm.predict(text="warm"):
            pass
        warm.close()
        for mode in ("predict", "constructor"):
            with self.subTest(mode=mode):
                client = self.client("constructor-hang" if mode == "constructor" else "ok", timeout=1)
                prior = len(self.events())
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    client.predict(text="hang")
                self.assertLess(time.monotonic() - started, 4)
                events = self.events()[prior:]
                self.assertTrue(any(row["event"] == "blocked-" + mode for row in events),
                                "The non-daemon SDK worker must actually block before this timeout is evidence")
                self.assert_stopped({row["pid"] for row in events})
                self.assertIsNone(client._resources.process)
                self.assertIsNone(client._resources.directory)
                if mode == "predict":
                    with client.predict(text="recovered"):
                        pass

    def test_queued_call_timeout_does_not_kill_the_other_active_call(self):
        client = self.client()
        with concurrent.futures.ThreadPoolExecutor(1) as executor:
            first = executor.submit(client.predict, text="wait")
            deadline = time.monotonic() + 2
            while not any(row["event"] == "call" for row in self.events()) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(any(row["event"] == "call" for row in self.events()))
            with transport.ensure_external_request_control(time.monotonic() + .05, threading.Event()):
                with self.assertRaises(TimeoutError):
                    client.predict(text="must-not-dispatch")
            (self.root / "release").touch()
            with first.result(3) as result:
                self.assertTrue(Path(result[0]).is_file())
        self.assertEqual(["wait"], [r["text"] for r in self.events() if r["event"] == "call"])

    def test_cancelled_request_never_starts_and_bad_protocol_response_stops_worker(self):
        client = self.client()
        cancelled = threading.Event(); cancelled.set()
        with transport.ensure_external_request_control(time.monotonic() + 3, cancelled):
            with self.assertRaises(TimeoutError):
                client.predict(text="cancelled")
        self.assertEqual([], self.events())
        self.assertIsNone(client._resources.process)
        with patch.object(transport, "get_bounded_json", return_value={"version": transport.PROTOCOL_VERSION, "id": "foreign", "audio": "audio.wav"}):
            with self.assertRaisesRegex(ValueError, "mismatched"):
                client.predict(text="bad-response")
        self.assert_stopped({row["pid"] for row in self.events()})
        self.assertIsNone(client._resources.process)
        with patch.object(transport, "get_bounded_json", return_value={"version": 0}):
            with self.assertRaisesRegex(ValueError, "protocol version"):
                client.predict(text="bad-version")
        self.assert_stopped({row["pid"] for row in self.events()})
        self.assertIsNone(client._resources.process)

    def test_native_parent_exit_reaps_sdk_interpreter_with_blocked_non_daemon_work(self):
        script = (
            "import json,time\n"
            "from pathlib import Path\n"
            "from external_tts_client import ExternalTTSClient\n"
            "from external_tts_executor import ExternalTTSExecutor\n"
            f"events=Path({str(self.root / 'events.jsonl')!r})\n"
            "client=ExternalTTSClient('http://fixture/ok',timeout=30)\n"
            "pool=ExternalTTSExecutor(1)\n"
            "pool.submit(client.predict,text='hang')\n"
            "deadline=time.monotonic()+5\n"
            "while time.monotonic()<deadline:\n"
            " if events.exists() and any(json.loads(r)['event']=='blocked-predict' for r in events.read_text().splitlines()): break\n"
            " time.sleep(.01)\n"
            "else: raise RuntimeError('SDK did not start')\n"
            "print('native-sdk-blocked-parent-exit',flush=True)\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                                env=self.environment, capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("native-sdk-blocked-parent-exit", result.stdout)
        events = self.events()
        self.assertTrue(any(row["event"] == "blocked-predict" for row in events))
        self.assert_stopped({row["pid"] for row in events})

    def test_protocol_bounds_and_provider_error_redaction_preserve_recovery(self):
        client = self.client()
        with self.assertRaisesRegex(ValueError, "byte limit"):
            client.predict(text="x" * transport.MAX_REQUEST_BYTES)
        self.assertEqual([], self.events())
        with self.assertRaises(RuntimeError) as caught:
            client.predict(text="error")
        self.assertNotIn("private-fixture-secret", str(caught.exception))
        self.assertIsNone(client._resources.process)
        with client.predict(text="recovered"):
            pass
        with patch.object(transport, "MAX_RESPONSE_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                client.predict(text="too-large-response")
        self.assertIsNone(client._resources.process)
        self.assert_stopped({row["pid"] for row in self.events()})

    def test_abrupt_native_parent_death_reaps_blocked_sdk_worker(self):
        script = (
            "import json,os,time\n"
            "from pathlib import Path\n"
            "from external_tts_client import ExternalTTSClient\n"
            "from external_tts_executor import ExternalTTSExecutor\n"
            f"events=Path({str(self.root / 'events.jsonl')!r})\n"
            "client=ExternalTTSClient('http://fixture/ok',timeout=30)\n"
            "pool=ExternalTTSExecutor(1)\n"
            "pool.submit(client.predict,text='hang')\n"
            "deadline=time.monotonic()+5\n"
            "while time.monotonic()<deadline:\n"
            " if events.exists() and any(json.loads(r)['event']=='blocked-predict' for r in events.read_text().splitlines()): break\n"
            " time.sleep(.01)\n"
            "else: raise RuntimeError('SDK did not block')\n"
            "os._exit(0)\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                                env=self.environment, capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        events = self.events()
        self.assertTrue(any(row["event"] == "blocked-predict" for row in events))
        self.assert_stopped({row["pid"] for row in events})

    def test_failed_stop_retains_owned_upload_bytes_until_reaping_is_proven(self):
        client = self.client()
        with client.predict(text='warm'):
            pass
        source = self.root / 'reference.wav'; source.write_bytes(b'admitted reference bytes')
        cancelled = threading.Event()
        def call():
            with transport.ensure_external_request_control(time.monotonic() + 5, cancelled):
                return client.predict({'path': str(source), 'meta': {'_type': 'gradio.FileData'}}, 'Reference', 'wait')
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            with patch.object(transport, 'stop_external_client_worker', side_effect=OSError('fixture termination failure')):
                future = pool.submit(call)
                try:
                    limit = time.monotonic() + 3
                    while not any(row.get('text') == 'wait' for row in self.events()) and time.monotonic() < limit:
                        time.sleep(.01)
                    calls = [row for row in self.events() if row.get('text') == 'wait']
                    self.assertTrue(calls, 'The SDK must own an active upload before cancellation')
                    owned = Path(calls[-1]['reference_path'])
                    cancelled.set()
                    with self.assertRaisesRegex(OSError, 'fixture termination failure'):
                        future.result(3)
                    self.assertIsNotNone(client._resources.process)
                    source.unlink()
                    self.assertTrue(owned.is_file(), 'Unreaped SDK work must retain its own immutable upload')
                    self.assertEqual(b'admitted reference bytes', owned.read_bytes())
                    workspace = Path(client._resources.directory.name)
                    pending = set(workspace.glob('request-*'))
                    with transport.ensure_external_request_control(time.monotonic() + .05, threading.Event()):
                        with self.assertRaisesRegex(OSError, 'fixture termination failure'):
                            client.predict(text='must-not-dispatch')
                    self.assertEqual(pending, set(workspace.glob('request-*')),
                                     'An unreaped SDK must refuse new work before allocating another request')
                    self.assertEqual(b'admitted reference bytes', owned.read_bytes())
                finally:
                    cancelled.set(); (self.root / 'release').touch()
        client.close()
        self.assert_stopped({row['pid'] for row in self.events()})


class SDKWindowsStopPolicyTests(unittest.TestCase):
    def test_headless_windows_sdk_stop_uses_owned_job_and_retains_unproven_ownership(self):
        for mode in ('success', 'terminate_error', 'query_error', 'still_active'):
            with self.subTest(mode=mode):
                active = [2]
                def count():
                    if mode == 'query_error': raise OSError('membership unknown')
                    return active[0]
                def terminate():
                    if mode == 'terminate_error': raise OSError('force kill refused')
                    if mode != 'still_active': active[0] = 0
                job = SimpleNamespace(handle=object(), get_active_process_count=count)
                owner = SimpleNamespace(job=job, terminate=Mock(side_effect=terminate),
                                        sendall=Mock(side_effect=OSError('headless taskkill refused')), close=Mock())
                process = SimpleNamespace(pid=123, _alexandria_windows_owner=owner,
                                          _alexandria_control=owner, wait=Mock(return_value=0))
                directory = SimpleNamespace(cleanup=Mock())
                resources = transport._ClientResources(); resources.process = process; resources.directory = directory
                with patch('subprocess_ownership.sys.platform', 'win32'), \
                        patch.object(transport.time, 'monotonic', side_effect=[0., 2.]):
                    if mode == 'success':
                        resources.stop()
                        self.assertIsNone(resources.process); self.assertIsNone(resources.directory)
                        process.wait.assert_called_once(); owner.close.assert_called_once(); directory.cleanup.assert_called_once()
                        owner.sendall.assert_not_called()
                        self.assertFalse(resources.pending_cleanup)
                    else:
                        with self.assertRaises(TimeoutError if mode == 'still_active' else OSError):
                            resources.stop()
                        self.assertIs(process, resources.process); self.assertIs(directory, resources.directory)
                        self.assertTrue(resources.pending_cleanup)
                        owner.close.assert_not_called(); directory.cleanup.assert_not_called()
