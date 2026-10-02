"""Assigned port receipt comes from the socket the disposable server retains."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import run_isolated_api_tests as runner


class IsolatedApiPortTests(unittest.TestCase):
    def test_native_bound_socket_is_retained_for_server_run(self):
        probe = '''
import errno,json,os,socket
from pathlib import Path
from unittest.mock import patch
import uvicorn
import run_isolated_api_tests as runner
observed=[]
def run(server,sockets=None):
 assert len(sockets)==1
 listener=sockets[0]
 port=runner.get_isolated_server_port(Path(os.environ['ALEXANDRIA_DATA_DIR']))
 assert listener.getsockname()==('127.0.0.1',port)
 assert listener.fileno()>=0
 with socket.socket() as competitor:
  try:competitor.bind(('127.0.0.1',port))
  except OSError as e:assert e.errno==errno.EADDRINUSE,e
  else:raise AssertionError('competitor acquired live server port')
 observed.append(listener)
with patch.object(uvicorn.Server,'run',run):runner.run_isolated_server()
assert len(observed)==1 and observed[0].fileno()==-1
print('PORT_OWNERSHIP='+json.dumps({'port':runner.get_isolated_server_port(Path(os.environ['ALEXANDRIA_DATA_DIR'])),'same_bound_socket':True,'competitor_rejected':True,'closed_after_server_return':True}))
'''
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable,'-c',probe], cwd=Path(runner.__file__).parent,
                                    env=dict(os.environ,ALEXANDRIA_DATA_DIR=tmp),
                                    capture_output=True,text=True,timeout=20)
            self.assertEqual(0,result.returncode,result.stderr)
            line=next(line for line in result.stdout.splitlines() if line.startswith('PORT_OWNERSHIP='))
            self.port_artifact=json.loads(line.split('=',1)[1])

    def test_metadata_missing_invalid_and_valid_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);receipt=root/'server_port.json'
            self.assertIsNone(runner.get_isolated_server_port(root))
            for value in (None,True,False,0,-1,65536,123.0,'123',[],{}):
                receipt.write_text(json.dumps({'port':value}))
                with self.subTest(value=value),self.assertRaises(ValueError):
                    runner.get_isolated_server_port(root)
            for value in (1,65535):
                receipt.write_text(json.dumps({'port':value}))
                self.assertEqual(value,runner.get_isolated_server_port(root))
            receipt.write_text('{invalid')
            with self.assertRaises(json.JSONDecodeError):runner.get_isolated_server_port(root)

    def test_parent_failure_and_timeout_stop_only_owned_child_and_remove_state(self):
        for mode in ('timeout','invalid','exited'):
            with self.subTest(mode=mode):
                server=MagicMock();server.pid=12345678;server.poll.return_value=1 if mode=='exited' else None
                observed={}
                def launch(command,**kwargs):
                    observed['root']=Path(kwargs['env']['ALEXANDRIA_DATA_DIR'])
                    self.assertEqual('0',kwargs['env']['ALEXANDRIA_PORT'])
                    self.assertEqual('127.0.0.1',kwargs['env']['ALEXANDRIA_HOST'])
                    return server
                def port(path):
                    if mode=='invalid':raise ValueError('invalid port receipt')
                    return None
                expected=ValueError if mode=='invalid' else RuntimeError
                with patch.object(runner.subprocess,'Popen',side_effect=launch), \
                     patch.object(runner,'get_isolated_server_port',side_effect=port), \
                     patch.object(runner.time,'monotonic',side_effect=[0,0,21]), \
                     patch.object(runner.time,'sleep'),patch.object(runner.os,'killpg') as kill, \
                     patch.object(runner,'urlopen',side_effect=AssertionError('unpublished port reached readiness')), \
                     patch.object(runner.subprocess,'run',side_effect=AssertionError('failed startup reached suite')):
                    with self.assertRaises(expected):runner.main()
                self.assertFalse(observed['root'].exists())
                if mode=='exited':kill.assert_not_called();server.wait.assert_not_called()
                else:kill.assert_called_once_with(server.pid,runner.signal.SIGTERM);server.wait.assert_called_once_with(timeout=10)
