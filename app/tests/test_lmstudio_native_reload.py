"""Authenticated CPU HTTP inventory and a real fake-CLI process verify reloads."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class NativeReloadTests(unittest.TestCase):
    def run_reload(self, revoke=False, generic=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded, calls = root / 'loaded', root / 'calls.jsonl'
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append((self.path, self.headers.get('Authorization')))
                    if self.path == '/props':
                        self.send_response(404); self.end_headers(); return
                    if revoke and loaded.exists():
                        self.send_response(401); self.end_headers(); return
                    if self.headers.get('Authorization') != 'Bearer fixture-token':
                        self.send_response(401); self.end_headers(); return
                    document = {'data': [{'object': 'model', 'id': 'alias'}]} if generic else {
                        'models': [{'type': 'llm', 'key': 'model-key', 'publisher': 'fixture',
                        'loaded_instances': [{'id': 'alias', 'config': {'context_length': 8192,
                           'parallel': 1}}] if loaded.exists() else []}]}
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps(document).encode())
                def log_message(self, *args):
                    pass
            server = HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cli = root / 'lms'
            cli.write_text('#!' + sys.executable + '\nimport json,pathlib,sys\n'
                + 'args=sys.argv[1:];state=pathlib.Path(' + repr(str(loaded)) + ')\n'
                + 'with open(' + repr(str(calls)) + ',"a") as f:f.write(json.dumps(args)+"\\n")\n'
                + 'if args[:2]==["server","status"]:print(json.dumps({"running":True,"port":'
                + str(server.server_port) + '}))\n'
                + 'elif args[0]=="ps":print(json.dumps([{"identifier":"alias","modelKey":"model-key",'
                + '"contextLength":8192,"parallel":1}] if state.exists() else []))\n'
                + 'elif args[0]=="load":state.write_text("loaded by CPU fixture")\n')
            cli.chmod(0o755)
            try:
                with patch.object(settings, 'find_lms_binary', return_value=str(cli)), \
                     patch.object(settings, 'get_local_vram_bytes', return_value=None):
                    _, status, message = settings.ensure_ideal_settings('local',
                        'http://127.0.0.1:%d/v1' % server.server_port,
                        'alias', api_key='fixture-token')
                operations = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
                return status, message, operations, requests
            finally:
                server.shutdown(); server.server_close(); thread.join(5)

    def test_authenticated_native_endpoint_reloads_and_fresh_state_verifies_it(self):
        status, message, operations, requests = self.run_reload()
        self.assertTrue(status['management_verified'])
        self.assertTrue(status['loaded']); self.assertTrue(status['optimized'])
        self.assertEqual(8192, status['context_length'])
        self.assertEqual(1, sum(args[0] == 'load' for args in operations))
        self.assertEqual(1, sum(args[0] == 'unload' for args in operations))
        native = [auth for path, auth in requests if path == '/api/v1/models']
        self.assertEqual(['Bearer fixture-token', 'Bearer fixture-token'], native)
        self.assertIn('Reloaded', message)

    def test_revoked_auth_after_cli_success_cannot_claim_verified_ideal_settings(self):
        status, message, operations, _ = self.run_reload(revoke=True)
        self.assertFalse(status['management_verified'])
        self.assertIn('fresh status did not verify', message)
        self.assertEqual(1, sum(args[0] == 'load' for args in operations))

    def test_generic_api_does_not_even_query_unrelated_cli(self):
        status, message, operations, _ = self.run_reload(generic=True)
        self.assertFalse(status['management_verified'])
        self.assertEqual([], operations)
        self.assertIn('not applied', message)
