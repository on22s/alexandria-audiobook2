from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import system


class ModelListEndpointPolicyTests(unittest.TestCase):
    def test_untrusted_urls_reject_before_client_or_saved_secret_lookup(self):
        app=FastAPI();app.include_router(system.router)
        with patch('llm_provider.make_llm_client') as make, \
             patch.object(system,'load_app_config') as load, TestClient(app) as client:
            for url in ('http://169.254.169.254/latest/meta-data', 'http://192.168.1.5:8080',
                        'https://untrusted.example/v1', 'http://localhost.untrusted.example',
                        'https://thundercompute.net.untrusted.example'):
                with self.subTest(url=url):
                    result=client.post('/api/llm/models',json={'base_url':url,'api_key':'fixture'})
                    self.assertEqual(400,result.status_code,result.text)
                    self.assertIn('not local',result.json()['detail'])
            make.assert_not_called();load.assert_not_called()

    def test_existing_loopback_and_thunder_hosts_keep_model_list_behavior(self):
        app=FastAPI();app.include_router(system.router)
        response=SimpleNamespace(models=SimpleNamespace(list=lambda:SimpleNamespace(data=[SimpleNamespace(id='fixture')])))
        with patch('llm_provider.make_llm_client',return_value=response) as make, TestClient(app) as client:
            for url in ('http://localhost:1234', 'http://127.0.0.2:1234/v1/',
                        'http://[::1]:1234', 'https://instance.thundercompute.net:1234'):
                with self.subTest(url=url):
                    result=client.post('/api/llm/models',json={'base_url':url,'api_key':'fixture'})
                    self.assertEqual(200,result.status_code,result.text)
                    self.assertEqual({'models':['fixture']},result.json())
                    self.assertEqual(system._normalize_openai_base_url(url),make.call_args.args[0]['base_url'])
