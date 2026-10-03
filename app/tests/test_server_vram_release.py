"""Owned campaign cleanup polls the shared VRAM policy with CPU stand-ins."""
from pathlib import Path
import os
import socket
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from tests.test_owned_server_cleanup import cleanup_fixture
from tests.test_gpu_lock_owner import run_owned_cpu_chain

ROOT=Path(__file__).resolve().parents[2]
CHAIN='qwen_local_rightsclean_budget_20260917.sh'


class ServerVramReleaseTests(unittest.TestCase):
    def fixture(self, root, policy, ignored=False, worker_rc=0):
        (root/'run_chains/lib').mkdir(parents=True)
        for helper in (ROOT/'run_chains/lib').glob('*.sh'): shutil.copy2(helper,root/'run_chains/lib'/helper.name)
        source=(ROOT/'run_chains'/CHAIN).read_text()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        source=source.replace('PORT=8098','PORT='+str(port))
        (root/'app/env/bin').mkdir(parents=True)
        for name in ('llama_server_process.py','subprocess_ownership.py'):
            shutil.copyfile(ROOT/'app'/name,root/'app'/name)
        script=root/'run_chains'/CHAIN;script.write_text(source)
        (root/'ab_test_runtime/adapters').mkdir(parents=True)
        (root/'ab_test_runtime/adapters/rightsclean.f16.gguf').write_bytes(b'CPU adapter not loaded')
        model=root/'model.gguf';model.write_bytes(b'CPU model not loaded')
        bin_dir=root/'bin';bin_dir.mkdir()
        def executable(path,code): path.write_text(code);path.chmod(0o755)
        server=root/'server.py';server.write_text(
            'import os,pathlib,signal,time,http.server\nroot=pathlib.Path(os.environ["FIXTURE_ROOT"])\n'
            'def stop(*args):\n (root/"term_seen").touch();time.sleep(.08);raise SystemExit(0)\n'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN if os.environ["IGNORE_TERM"]=="1" else stop)\n'
            '(root/"server.pid").write_text(str(os.getpid()))\n'
            'class Handler(http.server.BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write(b"healthy CPU fixture")\n def log_message(self,*args):pass\n'
            'http.server.HTTPServer(("127.0.0.1",int(os.environ["FIXTURE_PORT"])),Handler).serve_forever()\n')
        executable(root/'fake-server','#!/bin/bash\nexec '+sys.executable+' "'+str(server)+'"\n')
        for name,code in (('pkill','echo CPU-stop-stub'),('pgrep','exit 0'),('sleep','exec /bin/sleep .01'),
                          ('sha256sum','printf "c0dae304c51802e80000000000000000000000000000000000000000000000000000  fixture\\n"'),
                          ('git','if [ "${3:-}" = rev-parse ]; then echo "$FIXTURE_ROOT"; elif [ "${3:-}" = ls-files ]; then exit 1; else exit 0; fi')):
            executable(bin_dir/name,'#!/bin/bash\n'+code+'\n')
        executable(root/'gpu_job.sh','#!/bin/bash\nif [ "${1:-}" = --check-lock-owner ]; then exec bash REAL_GATE "$@"; fi\nif [ "${1:-}" = --check-vram ]; then exit 0; fi\nname="$1"; shift\nexec 9>"$GPU_LOCK"; flock -x 9 || exit 1\nexport ALEXANDRIA_GPU_LOCK_HELD=1 ALEXANDRIA_GPU_LOCK_PID=$$\nif [ "${GPU_RECLAIM_VRAM:-0}" = 1 ]; then\n    bash "$FIXTURE_ROOT/run_chains/lib/reclaim_vram.sh" "$FIXTURE_ROOT/gpu_job.sh" "$name" "$@" 9>&-\nelse\n    "$@" 9>&-\nfi\nexit "$?"\n'.replace('REAL_GATE',shlex.quote(str(ROOT/'gpu_job.sh'))))
        executable(root/'app/env/bin/python','#!/bin/bash\nif [ "${1:-}" = -B ]; then exec '+shlex.quote(sys.executable)+' "$@"; fi\necho "$*" >> "$FIXTURE_ROOT/jobs"\nexit "$WORKER_RC"\n')
        telemetry=root/'telemetry.py';telemetry.write_text(
            'import os,pathlib\nroot=pathlib.Path(os.environ["FIXTURE_ROOT"])\n'
            'p=root/"vram_queries";n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))\n'
            'pid=root/"server.pid"\n'
            'if pid.exists() and pathlib.Path("/proc",pid.read_text()).exists():(root/"queried_before_reap").touch()\n'
            'policy=os.environ["VRAM_POLICY"]\n'
            'if policy!="unknown":print("GPU[0] : VRAM Total Used Memory (B): "+str(1073741824 if policy=="release" and n>=3 else 2147483648 if policy=="release" and n==2 else 6442450944))\n')
        executable(bin_dir/'rocm-smi','#!/bin/bash\nexec '+sys.executable+' "'+str(telemetry)+'"\n')
        env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],'FIXTURE_ROOT':str(root),
             'HOME':str(root),'GPU_LOCK':str(root/'gpu.lock'),'MANAGED_SERVER_STOP_GRACE':'1','MANAGED_SERVER_PYTHON':sys.executable,'FIXTURE_PORT':str(port),
             'VRAM_POLICY':policy,'IGNORE_TERM':'1' if ignored else '0','WORKER_RC':str(worker_rc),
             'ALEXANDRIA_QWEN3_MODEL':str(model),'LLAMA_BIN':str(root/'fake-server'),'STAGE_VRAM_WAIT':'9' if policy=='release' else '6'}
        return script,env

    def test_whole_campaign_reaps_owned_child_then_polls_release_unknown_and_timeout_without_losing_worker_failure(self):
        for policy,ignored,worker_rc,expected in (('release',False,0,0),('release',True,0,0),
            ('unknown',False,0,0),('stuck',False,0,4),('release',False,2,1)):
            with self.subTest(policy=policy,ignored=ignored,worker_rc=worker_rc),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script,env=self.fixture(root,policy,ignored,worker_rc)
                try:
                    result=run_owned_cpu_chain(['bash',str(script)],root,env=env,capture_output=True,text=True,timeout=8)
                    self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                    pid=int((root/'server.pid').read_text())
                    self.assertFalse(Path('/proc',str(pid)).exists(),'child not reaped before campaign exit')
                    self.assertFalse((root/'queried_before_reap').exists(),'VRAM queried before owned child was reaped')
                    self.assertTrue((root/'vram_queries').exists(), result.stdout + result.stderr)
                    count=int((root/'vram_queries').read_text())
                    self.assertEqual(3 if policy=='release' else 1 if policy=='unknown' else 2,count)
                    self.assertEqual(4,len((root/'jobs').read_text().splitlines()))
                    output=result.stdout+result.stderr
                    if policy=='release':self.assertIn('VRAM reclaimed',output)
                    elif policy=='unknown':
                        self.assertIn('VRAM release unknown',output);self.assertNotIn('VRAM reclaimed',output)
                    else:
                        self.assertIn('VRAM still occupied',output);self.assertNotIn('VRAM reclaimed',output)
                    if ignored:self.assertIn('escalating',output)
                    else:self.assertTrue((root/'term_seen').exists())
                    if worker_rc:self.assertIn('FAILED: 4 of 4 stages',output)
                    self.assertEqual(b'CPU model not loaded',(root/'model.gguf').read_bytes())
                    self.assertEqual(b'CPU adapter not loaded',(root/'ab_test_runtime/adapters/rightsclean.f16.gguf').read_bytes())
                finally:
                    if (root/'server.pid').exists():cleanup_fixture(int((root/'server.pid').read_text()))

    def test_shared_stage_reclaim_uses_same_polling_and_retains_dispatch_after_cap_or_unknown_telemetry(self):
        for policy in ('release','unknown','stuck'):
            with self.subTest(policy=policy),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);_script,env=self.fixture(root,policy)
                (root/'script.sh').write_text('set -uo pipefail\nsource "$1/run_chains/lib/stage.sh"\n'
                    'STAGE_LOG_DIR="$1/logs"\nrun_stage CPU 1m --needs-vram -- touch "$FIXTURE_ROOT/dispatched"\n'
                    'stage_summary fixture\n')
                result=subprocess.run(['bash',str(root/'script.sh'),str(root)],env=env,capture_output=True,text=True,timeout=5)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertTrue((root/'dispatched').exists())
                self.assertEqual(3 if policy=='release' else 1 if policy=='unknown' else 2,int((root/'vram_queries').read_text()))
                output=result.stdout+result.stderr+(root/'logs/CPU.log').read_text()
                self.assertIn('reclaiming VRAM from configured llama-server',output)
                if policy=='unknown':self.assertIn('VRAM release unknown',output);self.assertNotIn('VRAM reclaimed',output)
                elif policy=='stuck':self.assertIn('VRAM still occupied',output);self.assertNotIn('VRAM reclaimed',output)
