"""Native HTTP checks for CORS around optional authentication."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import run_isolated_api_tests as runner


class CorsAuthOrderTests(unittest.TestCase):
    def test_native_allowed_denied_origins_and_optional_auth(self):
        actual_popen = subprocess.Popen
        actual_run = subprocess.run
        probe = """
import os, requests, json
from api_test_auth import get_api_test_headers
url=os.environ['FIXTURE_URL']
origin='http://localhost:4200'
protected=bool(os.environ.get('ALEXANDRIA_AUTH_PASSWORD'))
results=[]
for method in ('GET','POST'):
 path='/api/config' if method=='GET' else '/api/no-such-cors-fixture'
 r=requests.request(method,url+path,headers={'Origin':origin},timeout=10)
 assert r.status_code==(401 if protected else (200 if method=='GET' else 404)),r.status_code
 assert r.headers.get('Access-Control-Allow-Origin')==origin,dict(r.headers)
 assert r.headers.get('Access-Control-Allow-Credentials')=='true'
 if protected:
  assert r.headers.get('WWW-Authenticate')=='Basic realm="Alexandria"'
  assert 'www-authenticate' in r.headers.get('Access-Control-Expose-Headers','').lower()
 results.append({'method':method,'status':r.status_code,'cors':r.headers.get('Access-Control-Allow-Origin')})
headers=dict(get_api_test_headers(),Origin=origin)
r=requests.get(url+'/api/config',headers=headers,timeout=10)
assert r.status_code==200 and r.headers.get('Access-Control-Allow-Origin')==origin
for source,status in ((origin,200),('https://unapproved.invalid',400)):
 r=requests.options(url+'/api/config',headers={'Origin':source,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Authorization,Content-Type'},timeout=10)
 assert r.status_code==status,(status,r.status_code)
 assert r.headers.get('Access-Control-Allow-Origin')==(origin if status==200 else None)
for auth in ({},get_api_test_headers()):
 r=requests.get(url+'/api/config',headers=dict(auth,Origin='https://unapproved.invalid'),timeout=10)
 assert r.headers.get('Access-Control-Allow-Origin') is None
 assert r.status_code==(401 if protected and not auth else 200)
print('CORS_RESULTS='+json.dumps(results))
"""
        self.native_results = []
        for password in ('fixture-password', ''):
            with self.subTest(protected=bool(password)), tempfile.TemporaryFile() as server_log:
                observed = {}
                def launch(command, **kwargs):
                    if len(command) > 2 and command[2] == runner.SERVER_CODE:
                        observed['data_dir'] = kwargs['env']['ALEXANDRIA_DATA_DIR']
                        kwargs['stdout'] = server_log
                        kwargs['stderr'] = subprocess.STDOUT
                    return actual_popen(command, **kwargs)
                def run_probe(command, **kwargs):
                    env = dict(kwargs['env'], FIXTURE_URL=command[command.index('--url')+1])
                    result = actual_run([sys.executable, '-c', probe], cwd=kwargs['cwd'], env=env,
                                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(0, result.returncode, result.stderr)
                    line = next(line for line in result.stdout.splitlines() if line.startswith('CORS_RESULTS='))
                    self.native_results.append({'protected':bool(password), 'requests':json.loads(line.split('=',1)[1])})
                    return result
                with patch.dict(os.environ, {'ALEXANDRIA_AUTH_PASSWORD':password,
                                             'ALEXANDRIA_AUTH_USERNAME':'alexandria',
                                             'CORS_ORIGINS':'http://localhost:4200',
                                             'ALEXANDRIA_HOST':'127.0.0.1'}), \
                     patch.object(runner.subprocess, 'Popen', side_effect=launch), \
                     patch.object(runner.subprocess, 'run', side_effect=run_probe), \
                     patch.object(sys, 'argv', ['run_isolated_api_tests.py']):
                    self.assertEqual(0, runner.main())
                self.assertFalse(Path(observed['data_dir']).exists())
