"""Real SDK/HTTPX transport ownership: no network requests or inference."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import llm_provider as provider

if os.environ.get('FAILOVER_CLOSE_BASELINE'):
    path = Path(os.environ['FAILOVER_CLOSE_BASELINE'])
    exec(compile(path.read_text(), str(path), 'exec'), provider.__dict__)


class OwnedTransport(httpx.BaseTransport):
    def __init__(self):
        self.closes = 0

    def handle_request(self, request):
        raise AssertionError('cleanup fixture must not send requests')

    def close(self):
        self.closes += 1


class FailoverClientCleanupTests(unittest.TestCase):
    def sdk(self):
        transport = OwnedTransport()
        sdk = OpenAI(base_url='http://127.0.0.1:1/v1', api_key='local',
                     http_client=httpx.Client(transport=transport))
        self.addCleanup(sdk.close)
        return provider.ConfiguredOpenAI(sdk, {}), transport

    def test_close_releases_both_real_transports_before_and_after_switch(self):
        for switched in (False, True):
            with self.subTest(switched=switched):
                primary, primary_transport = self.sdk()
                secondary, secondary_transport = self.sdk()
                client = provider.FailoverClient(primary, 'p', secondary, 's')
                if switched:
                    client.failover({'category': 'connection_error'})
                client.close()
                self.assertTrue(primary.is_closed())
                self.assertTrue(secondary.is_closed())
                self.assertEqual((1, 1), (primary_transport.closes, secondary_transport.closes))
                client.close()
                self.assertEqual((1, 1), (primary_transport.closes, secondary_transport.closes))

    def test_timeout_variants_release_both_shared_sdk_pools_once(self):
        primary, p = self.sdk()
        secondary, s = self.sdk()
        client = provider.FailoverClient(primary, 'p', secondary, 's')
        variant = client.with_options(timeout=1)
        variant.failover({'category': 'timeout'})
        variant.close()
        self.assertTrue(primary.is_closed())
        self.assertTrue(secondary.is_closed())
        client.close()
        self.assertEqual((1, 1), (p.closes, s.closes))

    def test_shared_client_identity_is_closed_once(self):
        sdk, transport = self.sdk()
        client = provider.FailoverClient(sdk, 'p', sdk, 's')
        client.close()
        self.assertEqual(1, transport.closes)

    def test_one_close_error_does_not_abandon_other_client_and_can_retry(self):
        class Client:
            def __init__(self, fail=False):
                self.closes = 0
                self.fail = fail
                self.chat = SimpleNamespace(completions=SimpleNamespace())
            def close(self):
                self.closes += 1
                if self.fail:
                    self.fail = False
                    raise OSError('close failed')
        primary, secondary = Client(True), Client()
        client = provider.FailoverClient(primary, 'p', secondary, 's')
        with self.assertRaisesRegex(OSError, 'close failed'):
            client.close()
        self.assertEqual((1, 1), (primary.closes, secondary.closes))
        client.close()
        self.assertEqual((2, 1), (primary.closes, secondary.closes))

    def test_failed_secondary_construction_closes_created_primary(self):
        primary, transport = self.sdk()
        with patch('lmstudio_settings.get_failover_llm_config', return_value={'model_name': 's'}), \
             patch.object(provider, 'make_llm_client', side_effect=[primary, ValueError('bad profile')]):
            with self.assertRaisesRegex(ValueError, 'bad profile'):
                provider.make_run_client({'llm_failover': True}, {'model_name': 'p'}, 60)
        self.assertTrue(primary.is_closed())
        self.assertEqual(1, transport.closes)
