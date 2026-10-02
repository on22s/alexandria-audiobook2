"""Address equivalence selects the existing dispatch without DNS guesses."""
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class EndpointLocalityTests(unittest.TestCase):
    local = ('http://localhost.:1234/v1', 'http://LOCALHOST:1234/v1',
             'http://127.0.0.2:1234/v1', 'http://127.255.255.254:1234/v1',
             'http://[0:0:0:0:0:0:0:1]:1234/v1',
             'http://[::ffff:127.0.0.1]:1234/v1', 'http://[::1]:1234/v1',
             'http://127.0.0.1:1234/v1', 'http://0.0.0.0:1234/v1')

    def test_equivalent_loopback_spellings_select_local_profile(self):
        for url in self.local:
            with self.subTest(url=url):
                self.assertTrue(settings.is_local_llm_endpoint(url))
                self.assertFalse(settings.is_remote_llm('local', url))

    def test_explicit_remote_mode_still_wins_for_loopback_forwarding(self):
        for url in self.local:
            with self.subTest(url=url):
                self.assertTrue(settings.is_remote_llm('remote', url))

    def test_external_mapped_addresses_gateways_and_similar_names_are_remote(self):
        for host in ('host.docker.internal', 'localhost.example.com', 'localhost..',
                     '192.168.1.3', '10.0.0.2', '[::ffff:192.168.1.3]', '[2001:db8::1]'):
            with self.subTest(host=host):
                self.assertFalse(settings.is_local_llm_endpoint('http://' + host + '/v1'))
                self.assertTrue(settings.is_remote_llm('local', 'http://' + host + '/v1'))

    def test_empty_legacy_endpoint_preserves_local_default(self):
        for url in ('', None):
            self.assertTrue(settings.is_local_llm_endpoint(url))

    def test_real_status_dispatch_uses_local_probe_without_ssh_for_equivalent_addresses(self):
        for url in self.local:
            with self.subTest(url=url), \
                 patch.object(settings, 'get_llama_cpp_status', return_value=None), \
                 patch.object(settings, 'get_lmstudio_endpoint_status', return_value={'runtime': 'lmstudio'}), \
                 patch.object(settings, 'get_lmstudio_management_binding', return_value=(True, 1234, 'fixture verified')), \
                 patch.object(settings, 'get_lmstudio_status', return_value={'fixture': 'local'}) as local, \
                 patch.object(settings, 'get_remote_lmstudio_status') as remote, \
                 patch.object(settings, 'get_remote_runtime_status_cached') as cached:
                self.assertEqual('local', settings.get_current_status(
                    'local', url, 'model', 'stale-alias', use_cache=True)['fixture'])
                local.assert_called_once_with('model')
                remote.assert_not_called()
                cached.assert_not_called()
