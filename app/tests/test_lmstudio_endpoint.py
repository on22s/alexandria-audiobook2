"""Native HTTP fixtures reject generic responses and preserve authentication."""
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
import unittest

from lmstudio_endpoint import get_lmstudio_endpoint_status, get_native_model_status

V1 = {'models': [{'type': 'llm', 'key': 'model-key', 'publisher': 'fixture',
                 'loaded_instances': [{'id': 'alias', 'config': {
                     'context_length': 8192, 'parallel': 1}}]}]}
V0 = {'object': 'list', 'data': [{'object': 'model', 'type': 'llm',
      'id': 'alias', 'publisher': 'fixture', 'compatibility_type': 'gguf',
      'state': 'loaded', 'max_context_length': 32768}]}


class NativeEndpointTests(unittest.TestCase):
    def probe(self, replies, api_key=None):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get('Authorization')))
                code, document = replies.get(self.path, (404, {}))
                self.send_response(code)
                if code == 302:
                    self.send_header('Location', '/prefix/api/v0/models')
                self.end_headers()
                self.wfile.write(json.dumps(document).encode())
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = get_lmstudio_endpoint_status(
                'http://127.0.0.1:%d/prefix/v1' % server.server_port, 'alias', api_key)
            return status, calls
        finally:
            server.shutdown(); server.server_close(); thread.join(5)

    def test_current_native_schema_reports_exact_loaded_settings_and_alias(self):
        before = copy.deepcopy(V1)
        status, calls = self.probe({'/prefix/api/v1/models': (200, V1)}, 'fixture-token')
        self.assertEqual('lmstudio', status['runtime'])
        self.assertTrue(status['loaded'])
        self.assertEqual((8192, 1), (status['context_length'], status['parallel']))
        self.assertEqual([('/prefix/api/v1/models', 'Bearer fixture-token')], calls)
        self.assertEqual(before, V1)
        self.assertNotIn('management_verified', status, 'runtime identity cannot prove SSH ownership')

    def test_legacy_fallback_only_on_unsupported_current_endpoint(self):
        status, calls = self.probe({'/prefix/api/v0/models': (200, V0)})
        self.assertEqual(0, status['native_api_version'])
        self.assertTrue(status['loaded'])
        self.assertIsNone(status['context_length'])
        self.assertIsNone(status['parallel'])
        self.assertEqual(2, len(calls))

    def test_generic_and_malformed_responses_do_not_identify_lmstudio(self):
        for document in ({'data': [{'id': 'alias', 'object': 'model'}]}, [], None,
                         {'models': []}, {'models': [{'key': 'alias'}]}):
            with self.subTest(document=document):
                status, calls = self.probe({'/prefix/api/v1/models': (200, document)})
                self.assertIsNone(status)
                self.assertEqual(1, len(calls))

    def test_auth_and_service_failures_do_not_trigger_legacy_retry(self):
        for code in (401, 403, 500, 503):
            with self.subTest(code=code):
                status, calls = self.probe({'/prefix/api/v1/models': (code, {})})
                self.assertIsNone(status)
                self.assertEqual(1, len(calls))

    def test_authenticated_probe_does_not_follow_redirects_or_forward_token(self):
        status, calls = self.probe({'/prefix/api/v1/models': (302, {}),
                                   '/prefix/api/v0/models': (200, V0)}, 'fixture-token')
        self.assertIsNone(status)
        self.assertEqual([('/prefix/api/v1/models', 'Bearer fixture-token')], calls)

    def test_wrong_alias_and_multiple_instances_do_not_invent_loaded_context(self):
        status = get_native_model_status(V1, 'other-model', 1)
        self.assertFalse(status['loaded'])
        self.assertIsNone(status['context_length'])
        document = copy.deepcopy(V1)
        document['models'][0]['loaded_instances'].append({'id': 'second', 'config': {
            'context_length': 4096, 'parallel': 2}})
        status = get_native_model_status(document, 'model-key', 1)
        self.assertFalse(status['loaded'])
        self.assertEqual(8192, get_native_model_status(document, 'alias', 1)['context_length'])

    def test_invalid_instance_settings_are_unknown(self):
        for value in (None, True, 0, -1, '8192'):
            document = copy.deepcopy(V1)
            document['models'][0]['loaded_instances'][0]['config']['context_length'] = value
            self.assertIsNone(get_native_model_status(document, 'alias', 1))
