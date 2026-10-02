"""Concurrent server ensure calls serialize inspection, launch and publication."""
import ctypes
import fcntl
import os
from pathlib import Path
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LlamaLifecycleLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, libc.prctl(36, 1, 0, 0, 0))
        self.addCleanup(libc.prctl, 36, previous.value, 0, 0, 0)
        helper = self.root/'run_chains/lib/server_cleanup.sh'
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT/'run_chains/lib/server_cleanup.sh').read_bytes())
        validator = self.root/'app/llama_server_identity.py'
        validator.parent.mkdir()
        validator.write_bytes((ROOT/'app/llama_server_identity.py').read_bytes())
        for name in ('llama_server_process.py','subprocess_ownership.py'):
            (validator.parent/name).write_bytes((ROOT/'app'/name).read_bytes())
        self.script = self.root/'ensure.sh'
        source = (ROOT/'ensure_llama_server.sh').read_text()
        source = source.replace('STAMP="$HOME/.llama_server_adapter"', 'STAMP='+shlex.quote(str(self.root/'stamp')))
        self.script.write_text(source)
        model = self.root/'model.gguf'
        model.write_bytes(b'CPU fixture, not model weights')
        binary = self.root/'bin'
        binary.mkdir()
        scripts = {
            'curl': "import json,os,pathlib,sys,time\nr=pathlib.Path(os.environ['FIXTURE_ROOT']);(r/'inspected').touch()\nif not (r/'ready').exists():time.sleep(.08);raise SystemExit(1)\nurl=sys.argv[-1]\nresponse={'model_path':os.environ['LLAMA_MODEL'],'default_generation_settings':{'n_ctx':int(os.environ.get('LLAMA_CTX','32768')),'params':{'reasoning_format':'none'}},'total_slots':1} if url.endswith('/props') else ([] if url.endswith('/lora-adapters') else {'data':[{'id':os.environ.get('LLAMA_ALIAS','qwen3-14b'),'meta':{}}]})\nprint(json.dumps(response))\n",
            'sleep': 'import time;time.sleep(.01)\n',
            'pkill': "import os,pathlib; p=pathlib.Path(os.environ['FIXTURE_ROOT'])/'stops';p.open('a').write('stop\\n')\n",
            'llama-server': "import os,pathlib,time\nr=pathlib.Path(os.environ['FIXTURE_ROOT'])\nwith (r/'pids').open('a') as f:f.write(str(os.getpid())+'\\n')\ntry:os.fstat(8)\nexcept OSError:pass\nelse:(r/'inherited_lifecycle_fd').touch()\n(r/'ready').touch();time.sleep(30)\n",
        }
        flock = shutil.which('flock')
        self.assertIsNotNone(flock)
        scripts['flock'] = ("import os,pathlib,sys\npathlib.Path(os.environ['FIXTURE_ROOT'],'flock_attempted').touch()\n"
                            + 'os.execv('+repr(flock)+', ['+repr(flock)+', *sys.argv[1:]])\n')
        for name, content in scripts.items():
            p = binary/name
            p.write_text('#!'+sys.executable+'\n'+content)
            p.chmod(0o755)
        with socket.socket() as port_probe:
            port_probe.bind(('127.0.0.1',0))
            port = port_probe.getsockname()[1]
        self.env = dict(os.environ, LLAMA_PORT=str(port), FIXTURE_ROOT=str(self.root),
                        LLAMA_BIN=str(binary/'llama-server'), LLAMA_MODEL=str(model),
                        LLAMA_LOG=str(self.root/'server.log'),
                        PATH=str(binary)+os.pathsep+os.environ['PATH'])
        self.calls = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.calls:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        p = self.root/'pids'
        if p.exists():
            for pid in map(int,p.read_text().splitlines()):
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
                try:os.waitpid(pid,0)
                except ChildProcessError:pass

    def start(self):
        process = subprocess.Popen(['bash',str(self.script)],env=self.env,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.calls.append(process)
        return process

    def finish(self, process):
        out,err = process.communicate(timeout=5)
        self.assertEqual(0,process.returncode,out+err)
        return out

    def test_concurrent_identical_requests_load_once_and_reuse_same_server(self):
        first,second = self.start(),self.start()
        reports = [self.finish(first),self.finish(second)]
        self.assertEqual(1,len((self.root/'pids').read_text().splitlines()))
        self.assertEqual(1,sum('reusing server' in report for report in reports))
        self.assertFalse((self.root/'stops').exists(), 'startup must not request a global process-name kill')
        self.assertFalse((self.root/'inherited_lifecycle_fd').exists())
        self.assertIn('model=',(self.root/'stamp').read_text())
        self.finish(self.start())
        self.assertEqual(1,len((self.root/'pids').read_text().splitlines()))

    def test_external_lifecycle_holder_blocks_inspection_and_launch(self):
        with (self.root/'stamp.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            process = self.start()
            deadline = time.monotonic()+3
            while not (self.root/'flock_attempted').exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue((self.root/'flock_attempted').exists(), 'script reached the actual kernel lock gate')
            self.assertIsNone(process.poll())
            self.assertFalse((self.root/'inspected').exists())
            self.assertFalse((self.root/'pids').exists())
        self.finish(process)

    def test_invalid_lock_path_refuses_before_inspection_or_launch(self):
        (self.root/'stamp.lock').mkdir()
        process = self.start()
        out,err = process.communicate(timeout=5)
        self.assertEqual(2,process.returncode,out+err)
        self.assertIn('cannot acquire lifecycle lock',err)
        self.assertFalse((self.root/'inspected').exists())
        self.assertFalse((self.root/'pids').exists())

    def test_failed_flock_operation_refuses_before_inspection_or_launch(self):
        (self.root/'bin/flock').write_text('#!/bin/sh\nexit 1\n')
        process = self.start()
        out,err = process.communicate(timeout=5)
        self.assertEqual(2,process.returncode,out+err)
        self.assertIn('cannot acquire lifecycle lock',err)
        self.assertFalse((self.root/'inspected').exists())
        self.assertFalse((self.root/'pids').exists())
