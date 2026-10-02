"""Management must be positively bound to the configured native API."""
import json
import subprocess
import unittest
from unittest.mock import patch

import lmstudio_settings as settings


class ManagementBindingTests(unittest.TestCase):
    def test_custom_proxy_command_cannot_claim_direct_host_ownership(self):
        config = subprocess.CompletedProcess([], 0, 'hostname api.example\nproxycommand ssh other-host\n', '')
        with patch.object(settings.subprocess, 'run', return_value=config), \
             patch.object(settings, '_ssh_run') as ssh:
            self.assertFalse(settings.get_lmstudio_management_binding('http://api.example:5558/v1', 'alias')[0])
            ssh.assert_not_called()

    def test_remote_heal_uses_verified_nondefault_port_and_auth_for_fresh_reads(self):
        before = {'available': True, 'loaded': False, 'optimized': False}
        after = {'available': True, 'loaded': True, 'optimized': True, 'context_length': 98304, 'parallel': 2}
        config = subprocess.CompletedProcess([], 0, 'hostname api.example\n', '')
        with patch.object(settings, 'get_llama_cpp_status', return_value=None), \
             patch.object(settings, 'get_lmstudio_endpoint_status', return_value={'runtime': 'lmstudio'}) as native, \
             patch.object(settings.subprocess, 'run', return_value=config), \
             patch.object(settings, '_ssh_run', return_value=self.result({'running': True, 'port': 5558})), \
             patch.object(settings, 'get_remote_lmstudio_status', side_effect=[before, after]), \
             patch.object(settings, 'apply_remote_lmstudio_settings', return_value=(True, 'fixture reload')) as apply:
            _, status, _ = settings.ensure_ideal_settings('remote', 'http://api.example:5558/v1',
                                                        'alias', 'matching-host', api_key='fixture-key')
        apply.assert_called_once_with('matching-host', 'alias', ideal=True, port=5558)
        self.assertTrue(status['management_verified']); self.assertTrue(status['optimized'])
        self.assertEqual(['fixture-key', 'fixture-key'], [call.args[2] for call in native.call_args_list])

    def test_runtime_cache_separates_authentication_without_storing_plaintext_keys(self):
        settings.invalidate_remote_status_cache()
        self.addCleanup(settings.invalidate_remote_status_cache)
        with patch.object(settings, 'get_llama_cpp_status', return_value=None), \
             patch.object(settings, 'get_lmstudio_endpoint_status', return_value=None) as native:
            for key in ('first-fixture-key', 'first-fixture-key', 'second-fixture-key', 17, '17'):
                settings.get_current_status('remote', 'http://example/v1', 'alias', 'host',
                                            use_cache=True, api_key=key)
        self.assertEqual(4, native.call_count)
        self.assertNotIn('first-fixture-key', repr(settings._remote_status_cache))
        self.assertNotIn('second-fixture-key', repr(settings._remote_status_cache))

    def result(self, document, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(document), '')

    def test_local_direct_matching_port_is_verified_without_mutation(self):
        with patch.object(settings, 'find_lms_binary', return_value='/fixture/lms'), \
             patch.object(settings.subprocess, 'run', return_value=self.result(
                 {'running': True, 'port': 5558})) as run:
            verified, port, _ = settings.get_lmstudio_management_binding('http://localhost:5558/v1')
        self.assertTrue(verified)
        self.assertEqual(5558, port)
        self.assertEqual(['/fixture/lms', 'server', 'status', '--json', '--quiet'], run.call_args.args[0])

    def test_pretty_local_json_and_banner_prefixed_remote_json_are_supported(self):
        for text in (json.dumps({'running': True, 'port': 5558}, indent=2),
                     'decorative banner\n' + json.dumps({'running': True, 'port': 5558})):
            with self.subTest(text=text), \
                 patch.object(settings, 'find_lms_binary', return_value='/fixture/lms'), \
                 patch.object(settings.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')):
                self.assertTrue(settings.get_lmstudio_management_binding('http://localhost:5558/v1')[0])

    def test_wrong_stopped_bool_or_failed_cli_port_is_not_verified(self):
        for document in ({'running': True, 'port': 8090}, {'running': False, 'port': 5558},
                         {'running': True, 'port': True}, {}, {'running': 'true', 'port': 5558}):
            with self.subTest(document=document), \
                 patch.object(settings, 'find_lms_binary', return_value='/fixture/lms'), \
                 patch.object(settings.subprocess, 'run', return_value=self.result(document)):
                self.assertFalse(settings.get_lmstudio_management_binding('http://localhost:5558/v1')[0])

    def test_stale_remote_host_is_rejected_before_any_ssh_request(self):
        config = subprocess.CompletedProcess([], 0, 'hostname unrelated.example\n', '')
        with patch.object(settings.subprocess, 'run', return_value=config), \
             patch.object(settings, '_ssh_run') as ssh:
            verified, _, reason = settings.get_lmstudio_management_binding('http://api.example:5558/v1', 'stale-alias')
        self.assertFalse(verified)
        self.assertIn('does not match', reason)
        ssh.assert_not_called()

    def test_direct_remote_host_and_nondefault_port_are_verified(self):
        config = subprocess.CompletedProcess([], 0, 'hostname api.example\n', '')
        with patch.object(settings.subprocess, 'run', return_value=config), \
             patch.object(settings, '_ssh_run', return_value=self.result({'running': True, 'port': 5558})) as ssh:
            verified, port, _ = settings.get_lmstudio_management_binding('http://api.example:5558/v1', 'matching-alias')
        self.assertTrue(verified)
        self.assertEqual(5558, port)
        self.assertEqual(('matching-alias', 'lms server status --json --quiet'), ssh.call_args.args)

    def test_forwarded_and_proxied_addresses_cannot_assume_cli_ownership(self):
        for url, alias in (('http://localhost:5558/v1', 'remote-alias'),
                           ('https://api.example/proxy/v1', 'remote-alias')):
            with self.subTest(url=url), patch.object(settings.subprocess, 'run') as run, \
                 patch.object(settings, '_ssh_run') as ssh:
                self.assertFalse(settings.get_lmstudio_management_binding(url, alias)[0])
                run.assert_not_called(); ssh.assert_not_called()

    def test_generic_runtime_never_probes_or_mutates_any_management_host(self):
        for mode in ('local', 'remote'):
            with self.subTest(mode=mode), \
                 patch.object(settings, 'get_llama_cpp_status', return_value=None), \
                 patch.object(settings, 'get_lmstudio_endpoint_status', return_value=None), \
                 patch.object(settings, 'get_lmstudio_management_binding') as bind, \
                 patch.object(settings, 'apply_lmstudio_settings') as local, \
                 patch.object(settings, 'apply_remote_lmstudio_settings') as remote:
                _, status, message = settings.ensure_ideal_settings(mode, 'http://example/v1', 'model', 'stale')
                self.assertFalse(status['management_verified'])
                self.assertIn('not applied', message)
                bind.assert_not_called(); local.assert_not_called(); remote.assert_not_called()
