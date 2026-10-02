"""Actual ensure script against a local HTTP identity fixture, without model inference."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from tests import test_llama_lifecycle_lock as lifecycle


class LlamaReadinessIdentityTest(unittest.TestCase):
    start = lifecycle.LlamaLifecycleLockTest.start
    finish = lifecycle.LlamaLifecycleLockTest.finish
    cleanup_processes = lifecycle.LlamaLifecycleLockTest.cleanup_processes

    def setUp(self):
        lifecycle.LlamaLifecycleLockTest.setUp(self)
        # This fixture owns the HTTP transport in the test process, not a launched model.
        (self.root/'app/llama_server_process.py').write_text('raise SystemExit(0)\n')
        (self.root/'bin/curl').unlink()  # Exercise the actual curl HTTP transport.
        helper = self.root/'run_chains/lib/server_cleanup.sh'
        helper.write_text(helper.read_text().replace('grace="${2:-10}"','grace="${2:-0}"'))
        self.script.write_text(self.script.read_text().replace('seq 1 120','seq 1 2'))
        model = str(self.root/'model.gguf')
        self.valid = {'/v1/models':{'data':[{'id':'qwen3-14b','meta':{}}]},
                      '/props':{'model_path':model,'default_generation_settings':{'n_ctx':int(self.env.get('LLAMA_CTX','32768')),'params':{'reasoning_format':'none'}},'total_slots':1}, '/lora-adapters':[]}
        self.responses = copy.deepcopy(self.valid)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                value = owner.responses.get(self.path)
                self.send_response(503 if value is None else 200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join,5)
        self.addCleanup(server.shutdown)
        self.env['LLAMA_PORT'] = str(server.server_port)

    def assert_refused(self, adapter=None):
        if adapter is None:
            process = self.start()
        else:
            import subprocess
            process = subprocess.Popen(['bash',str(self.script),str(adapter)],env=self.env,
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            self.calls.append(process)
        out,err = process.communicate(timeout=5)
        self.assertEqual(1,process.returncode,out+err)
        self.assertIn('SERVER_NEVER_READY',err)
        self.assertFalse((self.root/'stamp').exists())

    def test_matching_base_and_exact_active_adapter_are_ready(self):
        self.finish(self.start())
        adapter = self.root/'adapter.gguf'
        adapter.write_bytes(b'CPU fixture adapter')
        self.responses['/lora-adapters'] = [{'id':0,'path':str(adapter),'scale':1.0}]
        from subprocess import run
        result = run(['bash',str(self.script),str(adapter)],env=self.env,
                     capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('adapter=',(self.root/'stamp').read_text())

    def test_successful_models_response_cannot_hide_wrong_model_alias_or_loading(self):
        for mode in ('model','alias','loading','malformed','props-unavailable'):
            with self.subTest(mode=mode):
                self.responses = copy.deepcopy(self.valid)
                if mode == 'model':self.responses['/props']['model_path'] = str(self.root/'other.gguf')
                elif mode == 'alias':self.responses['/v1/models']['data'][0]['id'] = 'other'
                elif mode == 'loading':self.responses['/v1/models']['data'][0]['meta'] = None
                elif mode == 'malformed':self.responses['/v1/models'] = {'data':[None]}
                else:self.responses['/props'] = None
                self.assert_refused()

    def test_requested_runtime_fields_are_checked_before_readiness(self):
        for mode in ('context', 'slots', 'reasoning', 'missing', 'bool-context'):
            with self.subTest(mode=mode):
                self.responses = copy.deepcopy(self.valid)
                props = self.responses['/props']
                if mode == 'context': props['default_generation_settings']['n_ctx'] = 4096
                elif mode == 'slots': props['total_slots'] = 2
                elif mode == 'reasoning': props['default_generation_settings']['params']['reasoning_format'] = 'auto'
                elif mode == 'missing': del props['default_generation_settings']
                else: props['default_generation_settings']['n_ctx'] = True
                self.assert_refused()

    def test_base_request_rejects_an_active_unrequested_adapter(self):
        self.responses['/lora-adapters'] = [{'id':0,'path':'other.gguf','scale':1.0}]
        self.assert_refused()

    def test_requested_adapter_rejects_wrong_missing_disabled_and_malformed_rows(self):
        adapter = self.root/'adapter.gguf'
        adapter.write_bytes(b'CPU fixture adapter')
        cases = [[], [{'id':0,'path':'wrong.gguf','scale':1}],
                 [{'id':0,'path':str(adapter),'scale':0}],
                 [{'id':0,'path':str(adapter),'scale':True}],
                 [{'id':0,'path':str(adapter),'scale':'1'}],
                 [None], {'id':0},
                 [{'id':0,'path':str(adapter),'scale':1}, {'id':1,'path':'extra.gguf','scale':1}]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.responses['/lora-adapters'] = rows
                self.assert_refused(adapter)
