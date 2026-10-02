"""Native loopback listeners prove exact selection and pidfd signaling safety."""
import ctypes
import json
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import llama_server_process as policy

SERVER = r"""
import ctypes,json,os,signal,socket,sys,time
from http.server import BaseHTTPRequestHandler,HTTPServer
ctypes.CDLL(None).prctl(15,b'llama-server',0,0,0)
if sys.argv[1]=='ignore':signal.signal(signal.SIGTERM,signal.SIG_IGN)
if sys.argv[1] in ('v6-loopback','v6-wildcard'):
 class IPv6Server(HTTPServer):
  address_family=socket.AF_INET6
  def server_bind(self):
   self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
   super().server_bind()
 server=IPv6Server(('::1' if sys.argv[1]=='v6-loopback' else '::',0),BaseHTTPRequestHandler)
else:server=HTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
child=None
if sys.argv[1]=='shared':
 child=os.fork()
 if child==0:time.sleep(30);os._exit(0)
print(json.dumps({'port':server.server_port,'child':child}),flush=True)
server.serve_forever()
"""


class LlamaServerProcessTest(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.children = []
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0,libc.prctl(37,ctypes.byref(previous),0,0,0))
        self.assertEqual(0,libc.prctl(36,1,0,0,0))
        self.addCleanup(libc.prctl,36,previous.value,0,0,0)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        import os
        for child in self.children:
            try:os.kill(child,signal.SIGKILL)
            except ProcessLookupError:pass
        for process in self.processes:
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        for child in self.children:
            try:os.waitpid(child,0)
            except ChildProcessError:pass

    def start(self, mode='normal'):
        process = subprocess.Popen([sys.executable,'-c',SERVER,mode],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.processes.append(process)
        state = json.loads(process.stdout.readline())
        if state['child']:self.children.append(state['child'])
        return process,state['port']

    def test_stop_selected_listener_preserves_same_named_server_on_other_port(self):
        target,port = self.start()
        sentinel,other_port = self.start()
        self.assertNotEqual(port,other_port)
        self.assertTrue(policy.stop_llama_server_listener(port,sys.executable))
        self.assertEqual(-signal.SIGTERM,target.wait(timeout=5))
        self.assertIsNone(sentinel.poll())
        self.assertTrue(policy.get_loopback_listener_inodes(other_port))

    def test_unknown_executable_refuses_without_signaling_listener(self):
        target,port = self.start()
        with self.assertRaisesRegex(RuntimeError,'another executable'):
            policy.stop_llama_server_listener(port,'/bin/true')
        self.assertIsNone(target.poll())

    def test_shared_listener_ownership_refuses_without_signaling_any_owner(self):
        target,port = self.start('shared')
        with self.assertRaisesRegex(RuntimeError,'ambiguous'):
            policy.stop_llama_server_listener(port,sys.executable)
        self.assertIsNone(target.poll())
        self.assertTrue(Path('/proc',str(self.children[0])).exists())

    def test_changed_birth_identity_refuses_before_signaling(self):
        target,port = self.start()
        birth = policy.get_process_identity(target.pid)
        with patch.object(policy,'get_process_identity',side_effect=[birth,'recycled']):
            with self.assertRaisesRegex(RuntimeError,'identity changed'):
                policy.stop_llama_server_listener(port,sys.executable)
        self.assertIsNone(target.poll())

    def test_term_refusing_selected_server_is_killed_after_bounded_grace(self):
        target,port = self.start('ignore')
        sentinel,_ = self.start()
        self.assertTrue(policy.stop_llama_server_listener(port,sys.executable,grace=.05))
        self.assertEqual(-signal.SIGKILL,target.wait(timeout=5))
        self.assertIsNone(sentinel.poll())

    def test_ipv6_loopback_on_same_number_is_not_the_configured_ipv4_endpoint(self):
        sentinel,port = self.start('v6-loopback')
        self.assertFalse(policy.stop_llama_server_listener(port,sys.executable))
        self.assertIsNone(sentinel.poll())

    def test_ipv6_wildcard_with_unknown_ipv4_routing_refuses_without_signaling(self):
        sentinel,port = self.start('v6-wildcard')
        with self.assertRaisesRegex(RuntimeError,'unverified IPv4 ownership'):
            policy.stop_llama_server_listener(port,sys.executable)
        self.assertIsNone(sentinel.poll())
