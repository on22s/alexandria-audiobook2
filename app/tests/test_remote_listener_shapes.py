"""Only the local ss listener field can prove a wildcard binding."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import lmstudio_settings as settings


class RemoteListenerShapeTests(unittest.TestCase):
    def test_all_network_wildcards_skip_redundant_server_start(self):
        for address in ('0.0.0.0:1234','[::]:1234',':::1234','*:1234'):
            for prefix in ('','tcp '):
                with self.subTest(address=address,prefix=prefix):
                    output='Decorative SSH banner\nState Recv-Q Send-Q Local Peer\n'+prefix+f'LISTEN 0 511 {address} *:*\n'
                    result=SimpleNamespace(returncode=0,stdout=output,stderr='')
                    with patch.object(settings,'_ssh_run',return_value=result) as ssh:
                        ok,message=settings.ensure_remote_server_running('fixture',1234)
                    self.assertTrue(ok)
                    self.assertIn('already bound',message)
                    ssh.assert_called_once_with('fixture','ss -tlnp',timeout=10,connect_timeout=10)

    def test_loopback_peer_banner_other_port_and_nonlistener_do_not_count(self):
        cases=('LISTEN 0 511 127.0.0.1:1234 0.0.0.0:1234 ',
               'LISTEN 0 511 [::1]:1234 *:*',
               'LISTEN 0 511 *:11234 *:*',
               'ESTAB 0 511 0.0.0.0:1234 *:*',
               'banner says server at 0.0.0.0:1234 ',
               'LISTEN 0 511 192.0.2.1:1234 *:*')
        for output in cases:
            with self.subTest(output=output),patch.object(settings,'_ssh_run',return_value=SimpleNamespace(returncode=0,stdout=output,stderr='')):
                self.assertFalse(settings._remote_server_bound('fixture',1234))

    def test_failed_probe_is_unknown(self):
        with patch.object(settings,'_ssh_run',return_value=SimpleNamespace(returncode=1,stdout='LISTEN 0 511 *:1234 *:*',stderr='fixture failure')):
            self.assertIsNone(settings._remote_server_bound('fixture',1234))
