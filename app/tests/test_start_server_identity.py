"""Real launcher, socket ownership and HTTP identity; no weights or inference."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]

SERVER = '''import json,os,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
args=sys.argv[1:]; port=int(args[args.index('--port')+1]); ctx=int(args[args.index('-c')+1])
Path('server.pid').write_text(str(os.getpid()))
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  override=json.loads(Path('override.json').read_text()) if Path('override.json').exists() else {}
  body=({'data':[{'id':'cpu-fixture','meta':{}}]} if self.path=='/v1/models' else
   override.get('adapters',[]) if self.path=='/lora-adapters' else
   {'model_path':override.get('model_path',str(Path('fixture_server').resolve())),
    'default_generation_settings':{'n_ctx':override.get('n_ctx',ctx)},'total_slots':1})
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(body).encode())
 def log_message(self,*args):pass
print('CPU fixture started',flush=True)
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


class StartServerIdentityTests(unittest.TestCase):
    def fixture(self, root):
        (root/'app').mkdir()
        (root/'run_chains/lib').mkdir(parents=True)
        for name in ('llama_server_process.py', 'llama_server_identity.py', 'subprocess_ownership.py'):
            shutil.copy2(ROOT/'app'/name, root/'app'/name)
        shutil.copy2(ROOT/'run_chains/lib/server_cleanup.sh', root/'run_chains/lib/server_cleanup.sh')
        shutil.copy2(ROOT/'start_llama_server.sh', root/'start_llama_server.sh')
        (root/'fixture_server').write_bytes(b'CPU fixture model marker')
        (root/'other_model').write_bytes(b'Another CPU fixture model marker')
        (root/'adapter.gguf').write_bytes(b'CPU fixture adapter marker')
        (root/'fixture_server.py').write_text(SERVER)
        (root/'bin').mkdir()
        sleeper = root/'bin/sleep'
        sleeper.write_text('#!/bin/bash\nexec /bin/sleep .02\n')
        sleeper.chmod(0o755)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        return {**os.environ, 'PATH':str(root/'bin')+os.pathsep+os.environ['PATH'],
                'PYTHONPATH':str(root), 'LLAMA_BIN':sys.executable,
                'LLAMA_MODEL':'fixture_server', 'LLAMA_PORT':str(port),
                'LLAMA_LOG':str(root/'server.log'), 'LLAMA_CTX':'16384',
                'LLAMA_KV':'q8_0', 'LLAMA_NGL':'99', 'LLAMA_ADAPTER':''}

    def run_launcher(self, root, env):
        return subprocess.run(['bash', str(root/'start_llama_server.sh')],
                              cwd=root, env=env, capture_output=True, text=True, timeout=15)

    def stop(self, root):
        if (root/'server.pid').exists():
            pid = int((root/'server.pid').read_text())
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    if Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].startswith('Z'):
                        return
                except FileNotFoundError:
                    return
                time.sleep(.01)
            self.fail('CPU fixture did not terminate')

    def test_native_reuse_requires_matching_model_adapter_context_kv_and_gpu_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.fixture(root)
            try:
                first=self.run_launcher(root,env)
                self.assertEqual(0,first.returncode,first.stdout+first.stderr)
                pid=(root/'server.pid').read_text(); log=(root/'server.log').read_bytes()
                again=self.run_launcher(root,env)
                self.assertEqual(0,again.returncode,again.stdout+again.stderr)
                self.assertIn('already up',again.stdout)
                import llama_server_process
                arguments = [os.fsdecode(value) for value in
                             Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[1:-1]]
                self.assertTrue(llama_server_process.is_expected_listener_launch(
                    int(env['LLAMA_PORT']), sys.executable, arguments))
                self.assertFalse(llama_server_process.is_expected_listener_launch(
                    int(env['LLAMA_PORT']), '/bin/bash', arguments))
                self.assertFalse(llama_server_process.is_expected_listener_launch(
                    int(env['LLAMA_PORT']), sys.executable, arguments+['--extra-setting']))
                for key,value in (('LLAMA_MODEL','other_model'),('LLAMA_ADAPTER',str(root/'adapter.gguf')),
                                  ('LLAMA_CTX','8192'),('LLAMA_KV','f16'),('LLAMA_NGL','0')):
                    with self.subTest(key=key):
                        rejected=self.run_launcher(root,{**env,key:value})
                        self.assertEqual(2,rejected.returncode,rejected.stdout+rejected.stderr)
                        self.assertIn('refusing reuse',rejected.stderr)
                        self.assertEqual(pid,(root/'server.pid').read_text())
                        self.assertEqual(log,(root/'server.log').read_bytes())
            finally:self.stop(root)

    def test_live_http_identity_changes_are_rejected_even_with_matching_process_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.fixture(root)
            try:
                first=self.run_launcher(root,env)
                self.assertEqual(0,first.returncode,first.stdout+first.stderr)
                for override in ({'model_path':str(root/'other_model')}, {'n_ctx':8192},
                                 {'adapters':[{'path':str(root/'adapter.gguf'),'scale':1}]}):
                    with self.subTest(override=override):
                        (root/'override.json').write_text(json.dumps(override))
                        rejected=self.run_launcher(root,env)
                        self.assertEqual(2,rejected.returncode,rejected.stdout+rejected.stderr)
            finally:self.stop(root)

    def test_new_launch_preserves_previous_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=self.fixture(root)
            old=b'previous failed load: diagnostic evidence\n'
            (root/'server.log').write_bytes(old)
            try:
                first=self.run_launcher(root,env)
                self.assertEqual(0,first.returncode,first.stdout+first.stderr)
                archives=[p for p in root.glob('server.log.*') if p.name!='server.log.lock']
                self.assertEqual(1,len(archives))
                self.assertEqual(old,archives[0].read_bytes())
                self.assertIn(b'CPU fixture started',(root/'server.log').read_bytes())
            finally:self.stop(root)
