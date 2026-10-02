"""Authenticated disposable server exercises actual readiness and client requests."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request
from unittest.mock import MagicMock
import unittest
from unittest.mock import patch
from api_test_auth import get_api_test_headers
import run_isolated_api_tests as runner
from tests import test_api as client


class IsolatedApiAuthTests(unittest.TestCase):
    def test_header_builder_preserves_inputs_and_optional_auth(self):
        headers={'X-Fixture':'value'}
        env={'ALEXANDRIA_AUTH_PASSWORD':'fixture-秘密','ALEXANDRIA_AUTH_USERNAME':'fixture-声'}
        built=get_api_test_headers(headers,env)
        self.assertEqual('fixture-声:fixture-秘密',base64.b64decode(built['Authorization'][6:]).decode('utf-8'))
        self.assertEqual({'X-Fixture':'value'},headers)
        self.assertEqual('fixture-秘密',env['ALEXANDRIA_AUTH_PASSWORD'])
        self.assertEqual(headers,get_api_test_headers(headers,{}))
        self.assertEqual('explicit',get_api_test_headers({'Authorization':'explicit'},env)['Authorization'])

    def test_authenticated_native_runner_and_client_keep_server_guard_enabled(self):
        # The suite subprocess is a narrow native client probe, not --full inference.
        self.scheme_case_artifacts=[]
        real_run=subprocess.run
        original_password=os.environ.get('ALEXANDRIA_AUTH_PASSWORD')
        probe='''
import os, requests
from tests import test_api as client
client.BASE_URL=os.environ['FIXTURE_URL']
r=requests.get(client.BASE_URL+'/api/config',timeout=10)
assert r.status_code==401, r.status_code
assert client.get('/api/config').status_code==200
from api_test_auth import get_api_test_headers
import json
credentials=get_api_test_headers()['Authorization'].split(' ',1)[1]
observed=[]
for scheme in ('Basic','basic','BASIC','bAsIc'):
 response=requests.get(client.BASE_URL+'/api/config',headers={'Authorization':scheme+' '+credentials},timeout=10)
 assert response.status_code==200,(scheme,response.status_code)
 observed.append({'scheme':scheme,'status':response.status_code})
for header in ('Bearer '+credentials,'basic !!!','BASIC bm9jb2xvbg=='):
 response=requests.get(client.BASE_URL+'/api/config',headers={'Authorization':header},timeout=10)
 assert response.status_code==401,response.status_code
 observed.append({'invalid_header':header.split(' ',1)[0],'status':response.status_code})
print('SCHEME_CASE_RESULTS='+json.dumps(observed))
assert client.post('/api/no-such-auth-fixture',json={}).status_code==404
assert client.delete('/api/no-such-auth-fixture').status_code==404
assert client.wait_for_task('audio',timeout=2,poll_interval=.01)
client.test_clone_voices_upload_bad_format()
'''
        for username in ('alexandria','fixture-声'):
            with self.subTest(username=username),tempfile.TemporaryFile(mode='w+b') as server_log:
                observed={}
                actual_popen=subprocess.Popen
                def launch(command,**kwargs):
                    if len(command) < 3 or command[2] != runner.SERVER_CODE:
                        return actual_popen(command, **kwargs)
                    self.assertEqual('fixture-秘密',kwargs['env']['ALEXANDRIA_AUTH_PASSWORD'])
                    observed['data_dir']=kwargs['env']['ALEXANDRIA_DATA_DIR']
                    kwargs['stdout']=server_log;kwargs['stderr']=subprocess.STDOUT
                    return actual_popen(command,**kwargs)
                def run_probe(command,**kwargs):
                    self.assertEqual('fixture-秘密',kwargs['env']['ALEXANDRIA_AUTH_PASSWORD'])
                    observed['url']=command[command.index('--url')+1]
                    env=dict(kwargs['env'],FIXTURE_URL=observed['url'])
                    result=real_run([sys.executable,'-c',probe],cwd=kwargs['cwd'],env=env,capture_output=True,text=True,timeout=20)
                    self.assertEqual(0,result.returncode,result.stderr)
                    line=next(line for line in result.stdout.splitlines() if line.startswith('SCHEME_CASE_RESULTS='))
                    self.scheme_case_artifacts.append({'username':username,'requests':json.loads(line.split('=',1)[1])})
                    return result
                with patch.dict(os.environ,{'ALEXANDRIA_AUTH_PASSWORD':'fixture-秘密','ALEXANDRIA_AUTH_USERNAME':username,'ALEXANDRIA_HOST':'127.0.0.1'}), \
                     patch.object(runner.subprocess,'Popen',side_effect=launch),patch.object(runner.subprocess,'run',side_effect=run_probe), \
                     patch.object(sys,'argv',['run_isolated_api_tests.py']):
                    self.assertEqual(0,runner.main())
                self.assertFalse(Path(observed['data_dir']).exists())
        self.assertEqual(original_password,os.environ.get('ALEXANDRIA_AUTH_PASSWORD'))

    def test_readiness_request_carries_auth_without_changing_environment(self):
        server=MagicMock();server.poll.return_value=None;server.pid=12345678
        def probe(request,**kwargs):
            self.assertIsInstance(request,Request)
            token=request.get_header('Authorization')
            self.assertEqual('alexandria:fixture',base64.b64decode(token[6:]).decode())
            return MagicMock()
        with patch.dict(os.environ,{'ALEXANDRIA_AUTH_PASSWORD':'fixture','ALEXANDRIA_AUTH_USERNAME':'alexandria'}), \
             patch.object(runner.subprocess,'Popen',return_value=server),patch.object(runner.subprocess,'run',return_value=type('Result',(),{'returncode':0})()), \
             patch.object(runner,'get_isolated_server_port',return_value=18765), \
             patch.object(runner,'urlopen',side_effect=probe),patch.object(runner.os,'killpg'),patch.object(sys,'argv',['runner']):
            self.assertEqual(0,runner.main())
            self.assertEqual('fixture',os.environ['ALEXANDRIA_AUTH_PASSWORD'])

    def test_client_native_request_sends_credentials_to_guarded_fixture(self):
        expected=get_api_test_headers(environ={'ALEXANDRIA_AUTH_PASSWORD':'fixture'})['Authorization']
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.headers.get('Authorization')==expected else 401)
                self.end_headers()
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict(os.environ,{'ALEXANDRIA_AUTH_PASSWORD':'fixture','ALEXANDRIA_AUTH_USERNAME':'alexandria'}), \
                 patch.object(client,'BASE_URL',f'http://127.0.0.1:{server.server_port}'):
                self.assertEqual(200,client.get('/guarded').status_code)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
