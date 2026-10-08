"""Managed server contract with native loopback HTTP and private kernel leases."""
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

from tests.test_gpu_lock_owner import run_owned_cpu_chain
from tests.test_owned_server_cleanup import ROOT, is_running, cleanup_fixture


class ManagedServerChainTests(unittest.TestCase):
    def execute(self, mode, sent_signal=None, duplicate=False, startup_delay=0):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);lib=root/'run_chains/lib';lib.mkdir(parents=True);app=root/'app';app.mkdir()
            for name in ('managed_server.sh','server_cleanup.sh','llm_campaign.sh'):
                shutil.copyfile(ROOT/'run_chains/lib'/name,lib/name)
            for name in ('llama_server_process.py','llama_server_identity.py','subprocess_ownership.py'):
                shutil.copyfile(ROOT/'app'/name,app/name)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=root/'server.py';server.write_text('''import http.server,os,pathlib,signal,subprocess,sys,time
root=pathlib.Path(os.environ['FIXTURE_ROOT']);(root/'server.pid').write_text(str(os.getpid()))
subprocess.run(['bash',str(root/'gpu_job.sh'),'--check-lock-owner',os.environ['ALEXANDRIA_GPU_LOCK_PID']],check=True)
time.sleep(float(os.environ.get('FIXTURE_STARTUP_DELAY','0')))
mode=os.environ['FIXTURE_MODE']
if mode=='exit':raise SystemExit(7)
if mode=='ignore':signal.signal(signal.SIGTERM,signal.SIG_IGN)
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(503 if mode=='ignore' else 200);self.end_headers();self.wfile.write(b'healthy fixture')
 def log_message(self,*args):pass
http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
''')
            attempts=100 if mode=='healthy' else 12
            program=root/'chain.sh';program.write_text('REPO='+shlex.quote(tmp)+'\n'+
                'source "$REPO/run_chains/lib/managed_server.sh" || exit 1\n'+
                'ensure_managed_server_lease fixture "$0" || exit 1\n'+
                'start_managed_server '+str(port)+' "$REPO/server.log" '+str(attempts)+' .02 '+shlex.quote(sys.executable)+' "$REPO/server.py" '+str(port)+' || exit 1\n'+
                'bash "$REPO/gpu_job.sh" --check-lock-owner "$ALEXANDRIA_GPU_LOCK_PID" || exit 1\n'+
                'printf "REQUESTS_ADMITTED\\n"\n'+
                ('kill -'+sent_signal+' $$\n' if sent_signal else '')+
                ('start_managed_server '+str(port)+' "$REPO/duplicate.log" 1 .01 touch "$REPO/duplicate-launched" || exit 1\n' if duplicate else ''))
            env=dict(os.environ,FIXTURE_ROOT=tmp,FIXTURE_MODE=mode,MANAGED_SERVER_PYTHON=sys.executable,
                     MANAGED_SERVER_STOP_GRACE='1', FIXTURE_STARTUP_DELAY=str(startup_delay))
            pid=None
            try:
                result=run_owned_cpu_chain(['bash',str(program)],root,env=env,capture_output=True,text=True,timeout=10)
                if (root/'server.pid').exists():pid=int((root/'server.pid').read_text())
                self.assertIsNotNone(pid,result.stdout+result.stderr)
                expected={'INT':130,'TERM':143,'HUP':129}.get(sent_signal,1 if duplicate or mode!='healthy' else 0)
                self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                if duplicate:
                    self.assertIn('already has a captured child',result.stderr)
                    self.assertFalse((root/'duplicate-launched').exists())
                    self.assertFalse((root/'duplicate.log').exists())
                self.assertFalse(is_running(pid))
                self.assertFalse(Path('/proc',str(pid)).exists(),'captured server child was not reaped')
                self.assertEqual(mode=='healthy','REQUESTS_ADMITTED' in result.stdout)
                if mode=='ignore':self.assertIn('escalating',result.stderr)
            finally:
                if pid is not None:cleanup_fixture(pid)

    def test_healthy_owned_listener_admits_requests_then_stops_and_reaps(self):self.execute('healthy')
    def test_early_exit_is_not_readiness_and_remains_reaped(self):self.execute('exit')
    def test_never_ready_term_ignoring_child_is_killed_and_reaped(self):self.execute('ignore')

    def test_signals_stop_and_reap_healthy_child_with_signal_status(self):
        for sent_signal in ('INT','TERM','HUP'):
            with self.subTest(signal=sent_signal):self.execute('healthy',sent_signal=sent_signal)

    def test_duplicate_launch_is_refused_without_overwriting_owned_pid(self):
        self.execute('healthy',duplicate=True)

    def test_shared_start_refuses_unowned_claim_before_launching_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);lib=root/'run_chains/lib';lib.mkdir(parents=True)
            for name in ('managed_server.sh','server_cleanup.sh','llm_campaign.sh'):
                shutil.copyfile(ROOT/'run_chains/lib'/name,lib/name)
            (root/'gpu_job.sh').write_text('exec bash '+shlex.quote(str(ROOT/'gpu_job.sh'))+' "$@"\n')
            program='REPO='+shlex.quote(tmp)+'; source "$REPO/run_chains/lib/managed_server.sh"; start_managed_server 8099 "$REPO/log" 1 .01 touch "$REPO/launched"'
            env=dict(os.environ,GPU_LOCK=str(root/'gpu.lock'),ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()),ALEXANDRIA_GPU_LOCK_HELD='1')
            (root/'gpu.lock').touch()
            result=subprocess.run(['bash','-c',program],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(4,result.returncode,result.stdout+result.stderr)
            self.assertFalse((root/'launched').exists())
            self.assertFalse((root/'log').exists())

    def test_healthy_fixture_waits_for_delayed_http_start_without_relaxing_owner_checks(self):
        self.execute('healthy', startup_delay=2)
