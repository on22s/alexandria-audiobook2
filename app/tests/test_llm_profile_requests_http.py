"""Typed diagnostic routes preserve edited provider profiles without inference."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from openai import OpenAI

from routers import system
import llm_provider


PROFILE = {
    'base_url': 'http://localhost:1234/', 'api_key': 'fixture',
    'model_name': 'chosen', 'provider_headers': {'X-Route': 'blue'},
    'provider_extra_body': {'routing': {'group': 'blue'}},
    'transport': 'http', 'request_timeout_seconds': 500,
    'connect_timeout_seconds': 3, 'request_interval_seconds': 0,
    'api_retry_limit': 2, 'on_api_exhaustion': 'pause',
    'retry_initial_delay_seconds': 1, 'retry_multiplier': 2,
    'retry_max_delay_seconds': 15, 'retry_jitter': 0.2,
    'reasoning_effort': 'low', 'on_this_gpu': False,
}


class LlmProfileRequestHttpTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(system.router)

    def test_models_preserve_provider_settings_legacy_packet_and_diagnostic_bound(self):
        calls = []

        def make(profile, **kwargs):
            calls.append((profile, kwargs))
            if len(calls) == 1:
                self.assertEqual({'X-Route': 'blue'}, profile.get('provider_headers'))
            return SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(
                data=[SimpleNamespace(id=x) for x in ['zeta', 'alpha', 'alpha']])))

        with patch('llm_provider.make_llm_client', side_effect=make), TestClient(self.app) as client:
            response = client.post('/api/llm/models', json=PROFILE)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({'models': ['alpha', 'zeta']}, response.json())
            legacy = client.post('/api/llm/models', json={'base_url': PROFILE['base_url']})
            self.assertEqual(200, legacy.status_code, legacy.text)
        for key, value in PROFILE.items():
            self.assertEqual('http://localhost:1234/v1' if key == 'base_url' else value,
                             calls[0][0][key], key)
        self.assertEqual({'timeout': 10, 'respect_profile_timeout': False}, calls[0][1])
        self.assertEqual('local', calls[1][0]['api_key'])

    def test_invalid_profile_rejected_before_any_provider_call(self):
        with patch('llm_provider.make_llm_client') as make, TestClient(self.app) as client:
            for field, value in [('provider_headers', []), ('provider_extra_body', []),
                                 ('transport', 'unknown'), ('request_timeout_seconds', 99999)]:
                for endpoint in ['models', 'test']:
                    with self.subTest(field=field, endpoint=endpoint):
                        response = client.post('/api/llm/' + endpoint, json={**PROFILE, field: value})
                        self.assertEqual(422, response.status_code, response.text)
            make.assert_not_called()

    def test_connection_route_passes_complete_profile_and_releases_claim(self):
        for transport in ['http', 'manual']:
            with self.subTest(transport=transport), \
                    patch.object(system, '_run_llm_test', return_value={'ok': True}) as probe, \
                    patch.object(system, 'claim_gpu_task', return_value='owned') as claim, \
                    patch.object(system, 'release_gpu_task_claim') as release, TestClient(self.app) as client:
                response = client.post('/api/llm/test', json={**PROFILE, 'transport': transport})
                self.assertEqual(200, response.status_code, response.text)
                actual = probe.call_args.args[0]
                for key, value in PROFILE.items():
                    expected = transport if key == 'transport' else (
                        'http://localhost:1234/v1' if key == 'base_url' else value)
                    self.assertEqual(expected, actual[key], key)
                claim.assert_called_once_with('llm_test')
                release.assert_called_once_with('llm_test', 'owned')

    def test_real_client_keeps_headers_and_body_while_diagnostics_keep_timeout_cap(self):
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()),
                              models=SimpleNamespace(list=lambda: SimpleNamespace(data=[])))
        with patch.object(llm_provider, 'OpenAI', return_value=sdk) as constructor:
            client = llm_provider.make_llm_client(PROFILE, timeout=10, respect_profile_timeout=False)
            client.models.list()
        self.assertEqual({'X-Route': 'blue'}, constructor.call_args.kwargs['default_headers'])
        self.assertEqual(10, constructor.call_args.kwargs['timeout'])
        self.assertEqual({**PROFILE['provider_extra_body'], 'reasoning_effort': 'low'},
                         client._provider_extra_body)

    def test_sdk_requests_include_provider_routing_and_completion_body(self):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual('blue', request.headers['X-Route'])
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'object': 'list', 'data': [
                    {'id': 'chosen', 'object': 'model', 'created': 0, 'owned_by': 'fixture'}]})
            return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion',
                'created': 0, 'model': 'chosen', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'pong'}, 'finish_reason': 'stop'}]})

        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            def make_sdk(**kwargs):
                return OpenAI(**kwargs, http_client=transport)

            with patch.object(llm_provider, 'OpenAI', side_effect=make_sdk), \
                    patch.object(system, 'claim_gpu_task', return_value='owned'), \
                    patch.object(system, 'release_gpu_task_claim'), TestClient(self.app) as client:
                models = client.post('/api/llm/models', json=PROFILE)
                self.assertEqual({'models': ['chosen']}, models.json())
                probe = client.post('/api/llm/test', json=PROFILE)
                self.assertEqual(200, probe.status_code, probe.text)
                self.assertTrue(probe.json()['ok'], probe.text)
                self.assertEqual('pong', probe.json()['reply'])
        self.assertEqual(['GET', 'GET', 'POST'], [r.method for r in requests])
        import json
        body = json.loads(requests[-1].content)
        self.assertEqual({'group': 'blue'}, body['routing'])
        self.assertEqual('low', body['reasoning_effort'])
        self.assertEqual('chosen', body['model'])
        self.assertEqual([10, 30, 30], [r.extensions['timeout']['read'] for r in requests])
