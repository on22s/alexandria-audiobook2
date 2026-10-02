"""Independent Gradio-client workers use native HTTP; each client remains locked."""
import concurrent.futures
import http.server
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
import tts


class ExternalTTSClientWorkerTests(unittest.TestCase):
    def test_one_endpoint_runs_two_requests_while_each_client_has_one_inflight_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);guard=threading.Lock();barrier=threading.Barrier(2);state={'active':0,'peak':0};clients=[]
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    with guard:
                        state['active']+=1;state['peak']=max(state['peak'],state['active'])
                    try:
                        try:barrier.wait(.2)
                        except threading.BrokenBarrierError:pass
                        body=b'{"sample":0.125}'
                        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                    finally:
                        with guard:state['active']-=1
                def log_message(self,*args):pass
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);server_thread=threading.Thread(target=server.serve_forever);server_thread.start()
            class Client:
                def __init__(self,url):
                    self.url=url;self.active=0;self.peak=0;self.count=0
                    with guard:self.index=len(clients);clients.append(self)
                def predict(self,**kwargs):
                    with guard:
                        self.active+=1;self.peak=max(self.peak,self.active);self.count+=1;number=self.count
                    try:
                        request=urllib.request.Request(self.url,data=json.dumps(kwargs).encode(),headers={'Content-Type':'application/json'})
                        with urllib.request.urlopen(request,timeout=3) as response:value=json.load(response)['sample']
                        path=root/f'client-{self.index}-{number}.wav';sf.write(path,np.full(240,value),24000)
                        return (str(path),)
                    finally:
                        with guard:self.active-=1
            url=f'http://127.0.0.1:{server.server_port}';engine=tts.TTSEngine({'tts':{'mode':'external','url':url,'parallel_workers':2,'external_timeout_seconds':5}})
            try:
                with patch.dict('sys.modules',{'gradio_client':SimpleNamespace(Client=Client)}),concurrent.futures.ThreadPoolExecutor(4) as pool:
                    futures=[pool.submit(engine.generate_custom_voice,f'line {i}','','A',{'A':{'voice':'Ryan','seed':0}},str(root/f'output-{i}.wav')) for i in range(4)]
                    self.assertTrue(all(future.result(6) for future in futures))
            finally:server.shutdown();server.server_close();server_thread.join(2)
            self.assertEqual(2,state['peak']);self.assertEqual(2,len(clients))
            self.assertEqual([1,1],[client.peak for client in clients]);self.assertEqual([2,2],[client.count for client in clients])
            for i in range(4):
                audio,rate=sf.read(root/f'output-{i}.wav');self.assertEqual(24000,rate);self.assertEqual(240,len(audio));np.testing.assert_allclose(audio,.125)
            self.assertEqual([],list(root.glob('*.pending.*')))
