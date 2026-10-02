"""Native local SSH adapter exercises the real remote control/receipt transport."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import benchmark_execution as execution
import benchmark_remote_execution as remote_execution
from benchmark_remote_command import get_remote_benchmark_receipt
from tests.test_benchmark_local_process_cancel import is_alive


@unittest.skipUnless(sys.platform=='linux','native Linux SSH adapter ownership proof')
class BenchmarkRemoteTransportTests(unittest.TestCase):
    def prepare(self, root, source):
        app=root/'app with spaces';app.mkdir()
        original=Path(__file__).resolve().parent.parent
        for name in ('benchmark_remote_command.py','subprocess_ownership.py'):
            (app/name).symlink_to(original/name)
        worker=app/'naming_benchmark.py';worker.write_text(source)
        ssh=root/'ssh';argv=root/'ssh-argv.jsonl'
        ssh.write_text('#!'+sys.executable+'\n'+
            'import json,os,pathlib,shlex,sys\n'+
            f'with pathlib.Path({str(argv)!r}).open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n'+
            'args=shlex.split(sys.argv[2]);os.execv(args[0],args)\n')
        ssh.chmod(0o700)
        return [str(ssh),'fixture-host',shlex.join([sys.executable,str(worker)])],argv

    def bind(self, state):
        token=execution.BENCHMARK_STATE.set(state)
        self.addCleanup(execution.BENCHMARK_STATE.reset,token)
        nonce=uuid.uuid4().hex
        receipt=get_remote_benchmark_receipt(nonce)
        self.addCleanup(shutil.rmtree,receipt.parent,ignore_errors=True)
        fixed=patch('benchmark_remote_execution.uuid.uuid4',return_value=SimpleNamespace(hex=nonce))
        fixed.start();self.addCleanup(fixed.stop)
        return receipt

    def test_normal_unicode_packet_is_private_and_exact_with_large_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state={'cancel':False}
            receipt=self.bind(state)
            command,argv=self.prepare(root,'import sys,time;time.sleep(.1);print(sys.stdin.read(),end="");print("stderr",file=sys.stderr)')
            payload='PRIVATE-音声🙂-'*30000
            result=execution.run_benchmark_subprocess(command,input=payload,capture_output=True,text=True,timeout=5,check=True)
            self.assertEqual(0,result.returncode)
            self.assertEqual(payload,result.stdout.split('\nBENCHMARK_OWNER_STOPPED=')[0])
            self.assertEqual('stderr\n',result.stderr)
            self.assertNotIn('PRIVATE-',argv.read_text())
            self.assertFalse(receipt.parent.exists(),'confirmed receipts must be cleaned up')
            self.assertEqual([],state['processes'])

    def test_cancel_timeout_and_connection_loss_confirm_detached_worker_cleanup(self):
        for action in ('cancel','timeout','disconnect'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);marker=root/'pids.json';state={'cancel':False}
                receipt=self.bind(state)
                source=f'''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
owner=os.getppid()
supervisor=int(pathlib.Path('/proc/'+str(owner)+'/stat').read_text().rsplit(')',1)[1].split()[1])
pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid,supervisor]))
time.sleep(30)
'''
                command,argv=self.prepare(root,source)
                sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
                def steer():
                    deadline=time.monotonic()+5
                    while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
                    if marker.exists():
                        if action=='cancel':state['cancel']=True
                        elif action=='disconnect':os.kill(json.loads(marker.read_text())[2],signal.SIGKILL)
                thread=threading.Thread(target=steer);thread.start()
                try:
                    if action=='cancel':
                        with self.assertRaises(execution.BenchmarkCancelled):
                            execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=5)
                    elif action=='timeout':
                        with self.assertRaises(subprocess.TimeoutExpired):
                            execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=.5)
                    else:
                        result=execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=5)
                        self.assertNotEqual(0,result.returncode)
                        self.assertIn('retaining the task',state['logs'][0])
                        self.assertEqual(3,len(argv.read_text().splitlines()),'connection loss must query then clean up the receipt')
                    self.assertTrue(marker.exists())
                    self.assertTrue(all(not is_alive(pid) for pid in json.loads(marker.read_text())[:2]))
                    self.assertFalse(receipt.parent.exists(),'confirmed receipts must be cleaned up')
                    self.assertEqual([],state['processes'])
                    self.assertIsNone(sentinel.poll())
                finally:
                    thread.join(timeout=6)
                    sentinel.terminate();sentinel.wait(timeout=5)
                    # Restore each invocation's state before the next subtest.
                    self.doCleanups()

    def test_decode_failure_still_confirms_shutdown_before_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state={'cancel':False};receipt=self.bind(state)
            command,_=self.prepare(root,'import sys;sys.stdout.buffer.write(b"\\xff")')
            with self.assertRaises(UnicodeDecodeError):
                execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=5)
            self.assertFalse(receipt.parent.exists(),'confirmed receipts must be cleaned up')
            self.assertEqual([],state['processes'])

    def test_lost_ack_wrong_nonce_and_probe_error_keep_process_registered_until_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state={'cancel':False};receipt=self.bind(state)
            command,_=self.prepare(root,'print("finished")')
            ssh=root/'ssh'
            ssh.write_text('#!'+sys.executable+'\n'+'''import os,shlex,subprocess,sys
args=shlex.split(sys.argv[2])
if '-c' in args:os.execv(args[0],args)
result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
sys.stdout.write(''.join(line for line in result.stdout.splitlines(True) if not line.startswith('BENCHMARK_OWNER_STOPPED=')))
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
''')
            native_run=subprocess.run;queries=[]
            def query(*args,**kwargs):
                self.assertEqual(1,len(state['processes']),'claim ownership cannot disappear before receipt verification')
                self.assertEqual('stopped\n',receipt.read_text())
                queries.append(args[0])
                if len(queries)==1:return subprocess.CompletedProcess(args[0],0,'BENCHMARK_RECEIPT='+'0'*32+'\n','')
                if len(queries)==2:raise OSError('fixture connection failure')
                return native_run(*args,**kwargs)
            with patch.object(remote_execution.subprocess,'run',side_effect=query):
                result=execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode)
            self.assertEqual(4,len(queries))
            self.assertEqual([],state['processes'])

    def test_cleanup_connection_failure_preserves_diagnostic_after_verified_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state={'cancel':False};receipt=self.bind(state)
            command,_=self.prepare(root,'print("finished")')
            def failed_cleanup(*args,**kwargs):
                self.assertIn('apply_remote_benchmark_receipt_cleanup',args[0][2])
                self.assertEqual('stopped\n',receipt.read_text())
                self.assertEqual(1,len(state['processes']))
                raise OSError('fixture cleanup connection failed')
            with patch.object(remote_execution.subprocess,'run',side_effect=failed_cleanup):
                result=execution.run_benchmark_subprocess(command,capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode)
            self.assertEqual('stopped\n',receipt.read_text())
            self.assertEqual([],state['processes'])
            self.assertIn('receipt cleanup connection failed',state['logs'][0])
