"""Supported hosted providers pass the same guards as Save and diagnostics."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx2
from openai import OpenAI

import core
import llm_provider
from routers import system


class HostedLlmEndpointTests(unittest.TestCase):
    def test_save_preview_diagnostics_and_cached_client_accept_official_origins(self):
        for url in ('https://api.deepseek.com/v1', 'https://api.openai.com/v1',
                    'https://openrouter.ai/api/v1'):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                profile = {'base_url':url,'api_key':'fixture','model_name':'chosen','on_this_gpu':False}
                payload = {'llm_mode':'remote','llm':profile,'llm_remote':profile,
                           'llm_local':{'base_url':'http://localhost:1234/v1','api_key':'local','model_name':'local'},
                           'tts':{'mode':'local'}}
                app = FastAPI();app.include_router(system.router)
                requests = []
                def respond(request):
                    requests.append(request)
                    self.assertEqual(httpx2.URL(url).host, request.url.host)
                    self.assertEqual('Bearer fixture', request.headers['authorization'])
                    if request.url.path.endswith('/models'):
                        return httpx2.Response(200,json={'object':'list','data':[
                            {'id':'chosen','object':'model','created':0,'owned_by':'fixture'}]})
                    return httpx2.Response(200,json={'id':'fixture','object':'chat.completion',
                        'created':0,'model':'chosen','choices':[{'index':0,
                        'message':{'role':'assistant','content':'pong'},'finish_reason':'stop'}]})
                with httpx2.Client(transport=httpx2.MockTransport(respond)) as transport:
                    def make_sdk(**kwargs):return OpenAI(**kwargs,http_client=transport)
                    with patch.object(llm_provider,'OpenAI',side_effect=make_sdk), \
                         patch.object(core,'_load_llm_config',return_value=profile), \
                         patch.object(core,'_llm_client_cache',{}), \
                         patch.object(system,'CONFIG_PATH',str(Path(directory,'config.json'))), \
                         patch.object(system.project_manager,'invalidate_config_cache'), \
                         patch.object(system.project_manager,'engine',None), \
                         patch.object(system,'claim_gpu_task',return_value='fixture-owner'), \
                         patch.object(system,'release_gpu_task_claim'), TestClient(app) as client:
                        preview = client.post('/api/config/preview',json=payload)
                        self.assertEqual(200,preview.status_code,preview.text)
                        saved = client.post('/api/config',json=payload)
                        self.assertEqual(200,saved.status_code,saved.text)
                        document = json.loads(Path(directory,'config.json').read_text())
                        self.assertEqual(url,document['llm']['base_url'])
                        models = client.post('/api/llm/models',json=profile)
                        self.assertEqual({'models':['chosen']},models.json())
                        probe = client.post('/api/llm/test',json=profile)
                        self.assertEqual(200,probe.status_code,probe.text)
                        self.assertTrue(probe.json()['ok'],probe.text)
                        configured, name = core._make_llm_client(timeout=10)
                        self.assertEqual('chosen',name)
                        self.assertIs(configured,core._make_llm_client(timeout=10)[0])
                        self.assertEqual('chosen',configured.models.list().data[0].id)
                self.assertEqual(['GET','GET','POST','GET'],[r.method for r in requests])

    def test_lookalikes_insecure_origins_and_untrusted_networks_remain_blocked(self):
        for url in ('http://api.deepseek.com/v1','https://api.openai.com:444/v1',
                    'https://api.deepseek.com.evil.test/v1','https://evil.api.deepseek.com/v1',
                    'https://openrouter.ai@evil.test/api/v1','https://user@api.openai.com/v1',
                    'https://api.openai.com/v1?redirect=evil','https://api.openai.com/v1#evil',
                    'https://api.openai.com/redirect','https://api.example.com/v1',
                    'http://192.168.1.2/v1','http://169.254.169.254/v1'):
            with self.subTest(url=url), self.assertRaises(core.LLMConfigError):
                core._validate_local_llm_base_url(url)


if __name__ == '__main__':unittest.main()
