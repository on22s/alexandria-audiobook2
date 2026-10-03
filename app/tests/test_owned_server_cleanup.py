"""Real Bash launchers own disposable CPU children, never inference servers."""
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]


def is_running(pid):
    try:
        status=Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[0]
        return status!='Z'
    except FileNotFoundError: return False


def cleanup_fixture(pid):
    if is_running(pid):
        os.kill(pid,signal.SIGKILL)
        deadline=time.monotonic()+3
        while is_running(pid) and time.monotonic()<deadline: time.sleep(.01)


class OwnedServerCleanupRaceTests(unittest.TestCase):
    def test_exit_during_escalation_ownership_query_is_reaped_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'race.sh'
            script.write_text('source "$1/run_chains/lib/server_cleanup.sh"\n'
                'sleep 60 & pid=$!\n'
                'queries=0\n'
                'is_owned_server_child() {\n'
                ' queries=$((queries + 1))\n'
                ' if [ "$queries" = 1 ]; then return 0; fi\n'
                ' wait "$1" 2>/dev/null || true\n'
                ' return 1\n'
                '}\n'
                'stop_owned_server "$pid" 0; rc=$?\n'
                'if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid"; wait "$pid"; exit 99; fi\n'
                '[ "$queries" = 2 ] || exit 98\n'
                'exit "$rc"\n')
            result = subprocess.run(['bash', str(script), str(ROOT)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class OwnedServerLauncherTests(unittest.TestCase):
    def fixture(self, root, script_name, mode):
        (root/'run_chains/lib').mkdir(parents=True)
        for helper in (ROOT/'run_chains/lib').glob('*.sh'):shutil.copy2(helper,root/'run_chains/lib'/helper.name)
        source=(ROOT/script_name).read_text()
        # Keep production's ten-second default; shorten grace only in CPU fixture.
        source=source.replace('stop_owned_server "$PID"','stop_owned_server "$PID" 1')
        source=source.replace('stop_owned_server "$SERVER_PID"','stop_owned_server "$SERVER_PID" 1')
        if script_name in ('ensure_llama_server.sh', 'start_llama_server.sh'):
            (root/'app').mkdir()
            for name in ('llama_server_identity.py','llama_server_process.py','subprocess_ownership.py'):
                (root/'app'/name).write_bytes((ROOT/'app'/name).read_bytes())
            source=source.replace('STAMP="$HOME/.llama_server_adapter"', 'STAMP='+shlex.quote(str(root/'stamp')))
            if script_name == 'start_llama_server.sh':
                # This module tests cleanup of shell-wrapped CPU children.
                # Native listener/executable verification is exercised by
                # test_start_server_identity with a real Python executable.
                (root/'app/llama_server_process.py').write_text('raise SystemExit(0)\n')
        script=root/script_name;script.write_text(source)
        model=root/'base.gguf';model.write_bytes(b'CPU model stand-in, never read as weights')
        bin_dir=root/'bin';bin_dir.mkdir()
        server=root/'cpu_server.py';server.write_text(
            'import os,pathlib,signal,time\n'
            'root=pathlib.Path(os.environ["FIXTURE_ROOT"]);mode=os.environ["SERVER_MODE"]\n'
            'def stop(signum,frame):\n'
            ' (root/"term_seen").touch();raise SystemExit(0)\n'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN if mode=="ignore" else stop)\n'
            '(root/"server.pid").write_text(str(os.getpid()))\n'
            'if mode=="exit":raise SystemExit(7)\n'
            'while True:time.sleep(.02)\n')
        server_bin=root/'fake-server';server_bin.write_text('#!/bin/bash\nexec '+sys.executable+' "'+str(server)+'"\n');server_bin.chmod(0o755)
        curl=bin_dir/'curl';curl.write_text('#!/bin/bash\n'
            + 'report() { python3 -c ' + shlex.quote(
                'import json,os,sys;url=sys.argv[-1];print(json.dumps('
                '{"model_path":os.environ["LLAMA_MODEL"],"default_generation_settings":{"n_ctx":int(os.environ.get("LLAMA_CTX","16384" if os.environ.get("FIXTURE_SCRIPT")=="start_llama_server.sh" else "32768")),"params":{"reasoning_format":"none"}},"total_slots":1} if url.endswith("/props") else '
                '([] if url.endswith("/lora-adapters") else {"data":[{"id":"qwen3-14b","meta":{}}]})))')
            + ' "$@"; }\n' +
            'n=$(cat "$FIXTURE_ROOT/queries" 2>/dev/null || echo 0);n=$((n+1));echo "$n" > "$FIXTURE_ROOT/queries"\n'
            'if [ "$n" -eq 1 ]; then exit 1; fi\n'
            'for _ in $(/usr/bin/seq 1 100); do [ -f "$FIXTURE_ROOT/server.pid" ] && break; /bin/sleep .01; done\n'
            'if [ "$SERVER_MODE" = delayed ] && [ "$n" -ge 3 ]; then report "$@"; exit 0; fi\n'
            'if [ "$SERVER_MODE" = delayed ] && [ -f "$FIXTURE_ROOT/stamp" ]; then report "$@"; exit 0; fi\n'
            'exit 1\n');curl.chmod(0o755)
        for name,body in (('seq','printf "1\\n2\\n3\\n"'),('sleep','exec /bin/sleep .01'),('pkill','printf "stop stub only\\n" >> "$FIXTURE_ROOT/stop_calls"')):
            path=bin_dir/name;path.write_text('#!/bin/bash\n'+body+'\n');path.chmod(0o755)
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],'FIXTURE_ROOT':str(root),
            'SERVER_MODE':mode,'FIXTURE_SCRIPT':script_name,'LLAMA_BIN':str(server_bin),'LLAMA_MODEL':str(model),'LLAMA_LOG':str(root/'server.log')}
        return script,env,model

    def run_case(self, script_name, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script,env,model=self.fixture(root,script_name,mode)
            sentinel=subprocess.Popen(['sleep','60'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            pid=None
            try:
                result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=8)
                pid=int((root/'server.pid').read_text())
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertFalse(is_running(pid),'readiness failure left launched child alive')
                self.assertFalse(Path('/proc',str(pid)).exists(),'owned child was not reaped')
                self.assertIsNone(sentinel.poll(),'unrelated child was stopped')
                self.assertEqual(b'CPU model stand-in, never read as weights',model.read_bytes())
                if mode=='cooperate':self.assertTrue((root/'term_seen').exists())
                if mode=='ignore':self.assertIn('escalating',result.stderr)
            finally:
                if (root/'server.pid').exists():pid=int((root/'server.pid').read_text());cleanup_fixture(pid)
                sentinel.terminate();sentinel.wait(timeout=3)

    def test_never_ready_owned_children_are_terminated_and_reaped_without_touching_sentinel(self):
        for script in ('ensure_llama_server.sh','start_llama_server.sh'):
            with self.subTest(script=script):self.run_case(script,'cooperate')

    def test_term_ignoring_owned_children_are_killed_and_reaped_after_bounded_grace(self):
        for script in ('ensure_llama_server.sh','start_llama_server.sh'):
            with self.subTest(script=script):self.run_case(script,'ignore')

    def test_immediately_exited_children_fail_and_are_reaped(self):
        for script in ('ensure_llama_server.sh','start_llama_server.sh'):
            with self.subTest(script=script):self.run_case(script,'exit')

    def test_delayed_readiness_leaves_healthy_server_alive_and_reuse_does_not_launch_another(self):
        for script_name in ('ensure_llama_server.sh','start_llama_server.sh'):
            with self.subTest(script=script_name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script,env,model=self.fixture(root,script_name,'delayed')
                pid=None
                try:
                    result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=8)
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    pid=int((root/'server.pid').read_text());self.assertTrue(is_running(pid))
                    self.assertFalse((root/'term_seen').exists())
                    # The endpoint's next probe succeeds, with the same launch stamp.
                    result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=8)
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(pid,int((root/'server.pid').read_text()))
                    self.assertTrue(is_running(pid));self.assertFalse((root/'term_seen').exists())
                    self.assertEqual(b'CPU model stand-in, never read as weights',model.read_bytes())
                finally:
                    if (root/'server.pid').exists():cleanup_fixture(int((root/'server.pid').read_text()))

    def test_shared_cleanup_refuses_unrelated_pid_and_invalid_values(self):
        helper=ROOT/'run_chains/lib/server_cleanup.sh'
        unrelated=subprocess.Popen(['sleep','60'])
        try:
            for value in ('', '0','-1','not-a-pid',str(unrelated.pid)):
                with self.subTest(value=value):
                    result=subprocess.run(['bash','-c','source "$1"; stop_owned_server "$2"','fixture',str(helper),value],
                        capture_output=True,text=True,timeout=3)
                    self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                    self.assertIn('REFUS',result.stderr)
                    self.assertIsNone(unrelated.poll())
        finally:unrelated.terminate();unrelated.wait(timeout=3)
