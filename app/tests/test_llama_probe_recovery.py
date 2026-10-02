"""Runtime classification recovers after failures without poisoning its miss cache."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error

import lmstudio_settings as settings
from tests.test_llama_cpp_status import PROPS, fake_props


class LlamaProbeRecoveryTests(unittest.TestCase):
    def setUp(self):
        with settings._props_miss_lock:
            settings._props_miss.clear()

    def tearDown(self):
        self.setUp()

    @contextlib.contextmanager
    def endpoint(self, replies):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                status, body = replies[min(len(calls) - 1, len(replies) - 1)]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}/v1', calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_native_transient_http_failures_and_invalid_json_recover_on_next_probe(self):
        cases = [(status, b'temporary failure') for status in (401, 403, 429, 500, 502, 503)]
        cases.append((200, b'{truncated'))
        for first in cases:
            with self.subTest(first=first), self.endpoint([
                    first, (200, json.dumps(PROPS).encode())]) as (url, calls):
                self.assertIsNone(settings.get_llama_cpp_status(url, 'qwen3-14b'))
                self.assertNotIn(url[:-3], settings._props_miss)
                status = settings.get_llama_cpp_status(url, 'qwen3-14b')
                self.assertIsNotNone(status)
                self.assertTrue(status['loaded'])
                self.assertEqual(32768, status['context_length'])
                self.assertEqual(['/props', '/props'], calls)

    def test_transport_errors_do_not_cache_a_runtime_decision(self):
        for error in (TimeoutError('timeout'), OSError('connection refused'),
                      urllib.error.URLError('disconnected')):
            with self.subTest(error=error):
                with patch('urllib.request.urlopen', side_effect=error):
                    self.assertIsNone(settings.get_llama_cpp_status('http://fixture/v1', 'qwen3-14b'))
                self.assertNotIn('http://fixture', settings._props_miss)
                with patch('urllib.request.urlopen', fake_props(PROPS)):
                    self.assertTrue(settings.get_llama_cpp_status('http://fixture/v1', 'qwen3-14b')['loaded'])

    def test_native_unsupported_routes_keep_existing_cache_and_expiry(self):
        for status in (404, 405, 501):
            with self.subTest(status=status), self.endpoint([
                    (status, b'unsupported'), (200, json.dumps(PROPS).encode())]) as (url, calls):
                for _ in range(5):
                    self.assertIsNone(settings.get_llama_cpp_status(url, 'qwen3-14b'))
                self.assertEqual(['/props'], calls)
                with settings._props_miss_lock:
                    settings._props_miss[url[:-3]] -= settings.PROPS_MISS_TTL_SECONDS + 1
                self.assertTrue(settings.get_llama_cpp_status(url, 'qwen3-14b')['loaded'])
                self.assertEqual(['/props', '/props'], calls)

    def test_public_status_dispatch_recovers_without_manual_cache_invalidation(self):
        with self.endpoint([(503, b'loading'), (200, json.dumps(PROPS).encode())]) as (url, calls):
            with patch.object(settings, 'get_lmstudio_endpoint_status', return_value=None):
                first = settings.get_current_status('local', url, 'qwen3-14b')
                self.assertEqual('unknown', first['runtime'])
                recovered = settings.get_current_status('local', url, 'qwen3-14b')
                self.assertEqual('llama.cpp', recovered['runtime'])
                self.assertTrue(recovered['loaded'])
                self.assertEqual(['/props', '/props'], calls)
