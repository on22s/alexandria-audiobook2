"""Actual remote worker/SDK HTTP request finishes before cooperative cancellation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import threading
import time
import unittest

import benchmark_execution as execution
from tests import test_benchmark_remote_transport_cancel as transport_tests


@unittest.skipUnless(sys.platform=='linux','native Linux cooperative remote worker proof')
class BenchmarkRemoteLlmCancelTests(unittest.TestCase):
    prepare = transport_tests.BenchmarkRemoteTransportTests.prepare
    bind = transport_tests.BenchmarkRemoteTransportTests.bind
    def test_current_sdk_request_finishes_then_cancellation_or_supervisor_loss_stops_remaining(self):
        for action in ('cancel','disconnect'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);state={'cancel':False};receipt=self.bind(state)
                command,_=self.prepare(root,'raise AssertionError("unused")')
                app=root/'app with spaces'
                original=Path(__file__).resolve().parent.parent
                worker_env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(original),os.environ.get('PYTHONPATH',''))))
                (app/'llm_benchmark_worker.py').write_text((original/'llm_benchmark_worker.py').read_text())
                pids=root/'pids.json'
                (app/'benchmark_runner.py').write_text(f'''import json,os,pathlib
import generate_script as gs
def _run_persona_case(fixture,client,model,context):
    owner=os.getppid()
    supervisor=int(pathlib.Path('/proc/'+str(owner)+'/stat').read_text().rsplit(')',1)[1].split()[1])
    pathlib.Path({str(pids)!r}).write_text(json.dumps([os.getpid(),supervisor]))
    return gs.call_llm_for_object(client,model,'Return JSON','Describe.',gs.LLMGenParams(max_tokens=400),label='COOPERATIVE FIXTURE',max_retries=3)
''')
                started=threading.Event();finished=threading.Event();calls=[]
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*args):pass
                    def do_POST(self):
                        calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        started.set();time.sleep(1)
                        response=json.dumps({'id':'fixture','object':'chat.completion','created':0,'model':'fixture',
                            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"description":"Calm.","ref_text":"Hello."}'}}]}).encode()
                        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response)));self.end_headers()
                        self.wfile.write(response);self.wfile.flush();finished.set()
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
                payload={'llm_config':{'base_url':f'http://127.0.0.1:{server.server_port}/v1','api_key':'fixture'},
                         'model_name':'fixture','fixtures':[{'id':str(i)} for i in range(3)],'repetitions':2}
                command[2]=shlex.join([sys.executable,str(app/'llm_benchmark_worker.py'),'--stage','persona_generation','--payload-stdin'])
                def steer():
                    if started.wait(5):
                        if action=='cancel':state['cancel']=True
                        else:os.kill(json.loads(pids.read_text())[1],signal.SIGKILL)
                steering=threading.Thread(target=steer);steering.start()
                try:
                    if action=='cancel':
                        try:
                            result=execution.run_benchmark_subprocess(command,input=json.dumps(payload),capture_output=True,text=True,timeout=8,env=worker_env)
                        except execution.BenchmarkCancelled:
                            pass
                        else:
                            self.fail('Cancellation did not propagate: '+result.stderr)
                    else:
                        result=execution.run_benchmark_subprocess(command,input=json.dumps(payload),capture_output=True,text=True,timeout=8,env=worker_env)
                        self.assertNotEqual(0,result.returncode)
                    self.assertTrue(started.is_set(),'actual SDK request must start: '+(result.stderr if action=='disconnect' else ''))
                    self.assertTrue(finished.is_set(),'current response must finish before shutdown proof')
                    self.assertEqual(1,len(calls),'remaining five requests and production retries must not start')
                    self.assertFalse(receipt.parent.exists(),'confirmed receipts must be cleaned up')
                    self.assertEqual([],state['processes'])
                finally:
                    steering.join(timeout=6)
                    server.shutdown();server.server_close();serving.join(timeout=2)
                    self.doCleanups()
