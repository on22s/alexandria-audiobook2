"""CPU listeners prove that healthy foreign endpoints cannot start evaluation."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import re
from pathlib import Path
import shlex
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import llama_server_process as policy
from tests.test_gpu_lock_owner import run_owned_cpu_chain

REPO = Path(__file__).resolve().parents[2]
CHAIN = REPO / 'run_chains/cloud_adapter_eval_remaining3_20260824.sh'


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'healthy CPU fixture')

    def log_message(self, *args):
        pass


class CloudAdapterReadinessTests(unittest.TestCase):
    def run_chain(self, mode, source=None, chain=CHAIN):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app/experiments').mkdir(parents=True)
            for name in ('llama_server_process.py', 'subprocess_ownership.py'):
                (root / 'app' / name).write_bytes((REPO / 'app' / name).read_bytes())
            library=root/'run_chains/lib';library.mkdir(parents=True)
            for name in ('managed_server.sh','llm_campaign.sh','server_cleanup.sh'):
                shutil.copyfile(REPO/'run_chains/lib'/name,library/name)
            model = root / 'model.gguf'
            model.write_bytes(b'CPU fixture model')
            adapter = root / 'adapter_speaker_hardcases_split_nonmajor.gguf'
            adapter.write_bytes(b'CPU fixture adapter')
            (root / 'app/experiments/lora_serving_eval_20260824.py').write_text(
                'from pathlib import Path\nPath("evaluated").write_text("CPU only")\n')
            wrappers = root / 'bin'
            wrappers.mkdir()
            server_code = root / 'server.py'
            server_code.write_text('''import os,sys,time
from http.server import BaseHTTPRequestHandler,HTTPServer
mode=os.environ['FIXTURE_MODE']
if mode=='dead':sys.exit(9)
if mode=='foreign':time.sleep(.3);sys.exit(9)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'healthy')
 def log_message(self,*args):pass
port=int(sys.argv[sys.argv.index('--port')+1])
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
''')
            for name, body in (
                    ('llama-server', 'exec ' + shlex.quote(sys.executable) + ' '
                     + shlex.quote(str(server_code)) + ' "$@"'),
                    ('sleep', 'exec /bin/sleep 0.01'),
                    ('nvidia-smi', 'echo "CPU fixture, 0 MiB"')):
                path = wrappers / name
                path.write_text('#!/bin/bash\n' + body + '\n')
                path.chmod(0o755)
            foreign = None
            if mode in ('foreign', 'dead'):
                foreign = HTTPServer(('127.0.0.1', 0), HealthHandler)
                port = foreign.server_port
                thread = threading.Thread(target=foreign.serve_forever, daemon=True)
                thread.start()
                self.assertTrue(policy.is_owned_loopback_listener(port, os.getpid()))
            else:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
            text = chain.read_text() if source is None else source
            for variable, value in (('REPO', str(root)), ('MODEL', str(model)),
                                    ('ADAPTER', str(adapter))):
                literal = re.search(r'(?m)^' + variable + r'="(?!\$)[^\n]*"$', text)
                if literal:
                    text = text[:literal.start()] + variable + '=' + shlex.quote(value) + text[literal.end():]
            text = text.replace('PORT=8090', 'PORT=' + str(port))
            (root/'app/experiments/three_pass_vs_single.py').write_text('from pathlib import Path\nPath("evaluated").write_text("CPU only")\n')
            script = root / 'chain.sh'
            script.write_text(text)
            try:
                result = run_owned_cpu_chain(['bash', str(script), 'speaker_hardcases_split_nonmajor'],root,
                    capture_output=True, text=True, timeout=10,
                    env=dict(os.environ, FIXTURE_MODE=mode,
                             PATH=str(wrappers) + os.pathsep + os.environ['PATH']))
                evaluated = (root / 'evaluated').exists()
                if foreign:
                    self.assertTrue(policy.is_owned_loopback_listener(port, os.getpid()),
                                    'cleanup must preserve the foreign listener')
                else:
                    self.assertFalse(policy.get_loopback_listener_inodes(port),
                                     'the chain must stop its own fixture server')
                self.assertEqual(b'CPU fixture adapter', adapter.read_bytes())
                return result, evaluated
            finally:
                if foreign:
                    foreign.shutdown()
                    foreign.server_close()
                    thread.join(timeout=5)

    def test_live_wrong_child_cannot_borrow_foreign_health(self):
        result, evaluated = self.run_chain('foreign')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertFalse(evaluated)

    def test_dead_child_cannot_borrow_foreign_health(self):
        result, evaluated = self.run_chain('dead')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertFalse(evaluated)

    def test_owned_healthy_child_evaluates_and_is_stopped(self):
        result, evaluated = self.run_chain('owned')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(evaluated)

    def test_balanced_and_pdnc_recipes_require_owned_health_and_clean_up(self):
        for name in ('cloud_balanced_eval_20260824.sh','cloud_pdnc_resume_20260824.sh'):
            for mode in ('owned','foreign','dead'):
                with self.subTest(recipe=name,mode=mode):
                    result,evaluated=self.run_chain(mode,chain=REPO/'run_chains'/name)
                    self.assertEqual(0 if mode=='owned' else 1,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(mode=='owned',evaluated)

    def test_listener_owner_cli_rejects_another_pid(self):
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        try:
            for pid, code in ((os.getpid(), 0), (1, 1), (0, 2)):
                result = subprocess.run([sys.executable, str(REPO / 'app/llama_server_process.py'),
                    '--check-listener-owner', str(server.server_port), str(pid)],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(code, result.returncode, result.stderr)
        finally:
            server.server_close()
