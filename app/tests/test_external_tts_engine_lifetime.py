"""Actual engine transport, publication, reset and reusable worker lifetime."""
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from tests.test_external_tts_client_process import FAKE_SDK, is_alive
from project import ProjectManager
import tts


class ExternalEngineLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'gradio_client.py').write_text(FAKE_SDK)
        self.environment = patch.dict(os.environ, PYTHONPATH=str(self.root),
                                      TMPDIR=str(self.root), TEMP=str(self.root), TMP=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.engine = tts.TTSEngine({'tts': {'mode': 'external', 'url': 'http://fixture/ok',
                                           'parallel_workers': 1, 'external_timeout_seconds': 3}})
        self.addCleanup(self.engine.close_external)
        self.config = {'A': {'voice': 'Ryan', 'seed': 123}}

    def events(self):
        path = self.root / 'events.jsonl'
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def wait_blocked(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = self.events()
            if any(row['event'] == 'blocked-predict' for row in events):
                return {row['pid'] for row in events}
            time.sleep(.01)
        self.fail('The SDK did not enter blocked non-daemon work')

    def assert_stopped(self, pids):
        limit = time.monotonic() + 3
        while any(is_alive(pid) for pid in pids) and time.monotonic() < limit:
            time.sleep(.01)
        self.assertTrue(all(not is_alive(pid) for pid in pids), pids)

    def render(self, kind, text):
        output = self.root / 'audio.wav'
        if kind == 'batch':
            output = self.root / 'temp_batch_0.wav'
            result = self.engine._external_batch([{'index': 0, 'speaker': 'A', 'text': text}],
                                                 self.config, str(self.root), 'custom')
            return result['completed'] == [0], output
        return self.engine.generate_custom_voice(text, '', 'A', self.config, str(output)), output

    def test_default_transport_recovers_single_and_batch_deadlines_with_bounded_pool(self):
        for kind in ('single', 'batch'):
            with self.subTest(kind=kind):
                # Warm the owned runtime, then retain its client across recovery.
                success, output = self.render(kind, 'warm')
                self.assertTrue(success)
                previous = output.read_bytes()
                executor = self.engine._external_executor
                client = next(iter(self.engine._gradio_clients.values()))
                self.engine._external_timeout = .3
                before = len(self.events())
                success, output = self.render(kind, 'hang')
                self.assertFalse(success)
                self.assertEqual(previous, output.read_bytes())
                events = self.events()[before:]
                self.assertTrue(any(row['event'] == 'blocked-predict' for row in events))
                self.assert_stopped({row['pid'] for row in events})
                self.engine._external_timeout = 3
                success, output = self.render(kind, 'recovered')
                self.assertTrue(success)
                self.assertIs(executor, self.engine._external_executor)
                self.assertIs(client, next(iter(self.engine._gradio_clients.values())))
                self.assertEqual(1, len(executor._threads))
                with wave.open(str(output), 'rb') as audio:
                    self.assertEqual((24000, 2400), (audio.getframerate(), audio.getnframes()))
                self.assertEqual([], list(Path(client._resources.directory.name).glob('request-*')))
                self.assertEqual([], list(self.root.glob('*.pending.*')))

    def test_engine_reset_stops_actual_active_sdk_and_preserves_prior_audio(self):
        self.assertTrue(self.render('single', 'warm')[0])
        output = self.root / 'audio.wav'; prior = output.read_bytes()
        manager = ProjectManager(str(self.root / 'runtime')); manager.engine = self.engine
        self.engine._external_timeout = 30
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.render, 'single', 'hang')
            pids = self.wait_blocked()
            started = time.monotonic(); manager.reset_engine()
            self.assertLess(time.monotonic() - started, 4)
            self.assertIsNone(manager.engine)
            self.assertFalse(future.result(4)[0])
        self.assert_stopped(pids)
        self.assertEqual(prior, output.read_bytes())
        self.assertEqual([], list(self.root.glob('*.pending.*')))
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.engine._ensure_external_executor()

    def test_reset_failure_retains_engine_ownership_and_closes_other_cached_clients(self):
        manager = ProjectManager(str(self.root / 'runtime')); manager.engine = self.engine
        class Client:
            def __init__(self, failure=False): self.failure = failure; self.closed = False
            def close(self):
                self.closed = True
                if self.failure: raise OSError('fixture stop failure')
        first, second = Client(True), Client()
        self.engine._gradio_clients = {('one', 0): first, ('two', 0): second}
        with self.assertRaisesRegex(OSError, 'fixture stop failure'):
            manager.reset_engine()
        self.assertIs(self.engine, manager.engine)
        self.assertTrue(first.closed); self.assertTrue(second.closed)
        first.failure = False
        manager.reset_engine()
        self.assertIsNone(manager.engine)

    def test_default_clone_transport_preserves_public_file_data_and_private_result_lifetime(self):
        reference = self.root / 'reference.wav'
        with wave.open(str(reference), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * 2400)
        config = {'A': {'type': 'clone', 'ref_audio': str(reference), 'ref_text': 'Reference text', 'seed': 123}}
        output = self.root / 'clone.wav'
        self.assertTrue(self.engine.generate_clone_voice('Clone speech', 'A', config, str(output)))
        client = next(iter(self.engine._gradio_clients.values()))
        self.assertEqual([], list(Path(client._resources.directory.name).glob('request-*')))
        calls = [row for row in self.events() if row['event'] == 'call']
        self.assertEqual(1, len(calls))
        self.assertEqual(reference.read_bytes().hex(), calls[0]['reference'])
        with wave.open(str(output), 'rb') as audio:
            self.assertEqual((24000, 2400), (audio.getframerate(), audio.getnframes()))
        self.assertEqual([], list(self.root.glob('*.pending.*')))

    def test_native_parent_exit_reaps_default_engine_sdk_work(self):
        script = (
            "import json,threading,time\n"
            "from pathlib import Path\n"
            "from tts import TTSEngine\n"
            f"events=Path({str(self.root / 'events.jsonl')!r})\n"
            f"output={str(self.root / 'native.wav')!r}\n"
            "engine=TTSEngine({'tts':{'mode':'external','url':'http://fixture/ok','parallel_workers':1,'external_timeout_seconds':30}})\n"
            "threading.Thread(target=engine.generate_custom_voice,args=('hang','','A',{'A':{'voice':'Ryan'}},output),daemon=True).start()\n"
            "deadline=time.monotonic()+5\n"
            "while time.monotonic()<deadline:\n"
            " if events.exists() and any(json.loads(r)['event']=='blocked-predict' for r in events.read_text().splitlines()): break\n"
            " time.sleep(.01)\n"
            "else: raise RuntimeError('SDK did not block')\n"
            "print('engine-exit-ready',flush=True)\n")
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).parents[1],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('engine-exit-ready', result.stdout)
        events = self.events()
        self.assertTrue(any(row['event'] == 'blocked-predict' for row in events))
        self.assert_stopped({row['pid'] for row in events})
        self.assertFalse((self.root / 'native.wav').exists())

    def test_clone_snapshot_lives_until_owned_sdk_stops_then_cleans_after_timeout(self):
        reference = self.root / 'reference.wav'
        with wave.open(str(reference), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * 2400)
        admitted = reference.read_bytes()
        config = {'A': {'type': 'clone', 'ref_audio': str(reference), 'ref_text': 'Reference', 'seed': 123}}
        output = self.root / 'clone.wav'
        self.assertTrue(self.engine.generate_clone_voice('Warm', 'A', config, str(output)))
        previous = output.read_bytes()
        self.engine._external_timeout = 1
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.engine.generate_clone_voice, 'hang', 'A', config, str(output))
            pids = self.wait_blocked()
            call = [row for row in self.events() if row['event'] == 'call'][-1]
            snapshot = Path(call['reference_path'])
            self.assertNotEqual(reference, snapshot)
            self.assertEqual(admitted, snapshot.read_bytes())
            reference.write_bytes(b'replaced source after admission')
            self.assertEqual(admitted, snapshot.read_bytes())
            self.assertFalse(future.result(4))
        self.assert_stopped(pids)
        limit = time.monotonic() + 3
        while snapshot.exists() and time.monotonic() < limit: time.sleep(.01)
        self.assertFalse(snapshot.exists())
        self.assertEqual(previous, output.read_bytes())
        self.assertEqual([], list(self.root.glob('*.pending.*')))


class NativeSDKConstructorDeadlineTests(unittest.TestCase):
    def test_installed_gradio_sdk_constructor_http_stall_is_bounded_and_reaped(self):
        import http.server
        arrived = threading.Event(); release = threading.Event(); requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path); arrived.set(); release.wait(10)
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever); serving.start()
        engine = tts.TTSEngine({'tts': {'mode': 'external', 'url': f'http://127.0.0.1:{server.server_port}',
                                      'parallel_workers': 1, 'external_timeout_seconds': 3}})
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / 'audio.wav'; output.write_bytes(b'prior approved audio')
                for _ in range(2):
                    arrived.clear(); started = time.monotonic()
                    self.assertFalse(engine.generate_custom_voice('Hello', '', 'A', {'A': {'voice': 'Ryan'}}, str(output)))
                    self.assertTrue(arrived.is_set(), 'The installed SDK must actually reach native HTTP')
                    self.assertLess(time.monotonic() - started, 6)
                    self.assertEqual(b'prior approved audio', output.read_bytes())
                self.assertEqual(1, len(engine._external_executor._threads))
                engine.close_external()
                self.assertTrue(all(client._resources.process is None for client in engine._gradio_clients.values()))
                self.assertEqual([], list(Path(temporary).glob('*.pending.*')))
            self.assertTrue(any('/config' in path for path in requests), requests)
        finally:
            release.set(); engine.close_external(); server.shutdown(); server.server_close(); serving.join(3)
