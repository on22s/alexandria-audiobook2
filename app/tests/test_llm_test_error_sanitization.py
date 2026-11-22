"""LLM test failures expose operation-only diagnostics, never provider text."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx2
from openai import OpenAI

from routers import system
import llm_provider


PROFILE = {
    'base_url': 'http://localhost:1234/v1', 'api_key': 'fixture',
    'model_name': 'chosen', 'transport': 'http', 'on_this_gpu': False,
}

# Synthetic values only: a real credential or copyrighted line must never be
# needed to prove that provider text stays out of responses and logs.
SECRET = 'SYNTHETIC_LLM_TEST_SECRET_4c1d9e2a'
LEAKY_DETAIL = (f'invalid key={SECRET} Authorization: Bearer {SECRET} '
                f'request_body={SECRET} url=http://host/v1/models?token={SECRET}')

MODELS = {'object': 'list', 'data': [
    {'id': 'chosen', 'object': 'model', 'created': 0, 'owned_by': 'fixture'}]}
REPLY = {'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': 'chosen',
         'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'pong'},
                      'finish_reason': 'stop'}]}


class LlmTestErrorSanitizationTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(system.router)

    def probe(self, responder, directory, profile=None):
        def make_sdk(**kwargs):
            return OpenAI(**kwargs, http_client=transport)

        with httpx2.Client(transport=httpx2.MockTransport(responder)) as transport, \
                patch.object(llm_provider, 'OpenAI', side_effect=make_sdk), \
                patch.object(system, 'API_LOG_DIR', str(directory)), \
                patch.object(system, 'claim_gpu_task', return_value='owned'), \
                patch.object(system, 'release_gpu_task_claim') as release, \
                TestClient(self.app) as client:
            response = client.post('/api/llm/test', json=profile or PROFILE)
        return response, release

    def assert_clean(self, response, release, step, models=None):
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertEqual(step, body['step'])
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn('invalid key=', response.text)
        if models is not None:
            self.assertEqual(models, body['models'])
        release.assert_called_once_with('llm_test', 'owned')
        log = body.get('log_file')
        self.assertTrue(log, body)
        text = Path(log).read_text(encoding='utf-8')
        self.assertNotIn(SECRET, text)
        self.assertNotIn('invalid key=', text)

    def test_models_exception_never_leaks_provider_text(self):
        def respond(request):
            self.assertEqual('GET', request.method)
            return httpx2.Response(401, json={'error': {'message': LEAKY_DETAIL,
                                                        'type': 'invalid_request_error'}})

        with tempfile.TemporaryDirectory() as directory:
            response, release = self.probe(respond, directory)
            self.assert_clean(response, release, 'models')
            self.assertEqual('Could not read the model list from the endpoint. '
                             'Check the base URL and credentials, then try again.',
                             response.json()['error'])

    def test_completion_exception_never_leaks_provider_text(self):
        def respond(request):
            if request.url.path.endswith('/models'):
                return httpx2.Response(200, json=MODELS)
            self.assertEqual('POST', request.method)
            return httpx2.Response(400, json={'error': {'message': LEAKY_DETAIL,
                                                        'type': 'invalid_request_error'}})

        with tempfile.TemporaryDirectory() as directory:
            response, release = self.probe(respond, directory)
            self.assert_clean(response, release, 'completion', models=['chosen'])
            self.assertTrue(response.json()['model_present'])
            self.assertEqual('The test request did not complete. '
                             'Check the endpoint and model, then try again.',
                             response.json()['error'])

    def test_validation_exception_never_leaks_provider_text(self):
        def respond(request):
            self.fail('validation failure must not reach the provider')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(system, '_validate_local_llm_base_url',
                             side_effect=system.LLMConfigError(LEAKY_DETAIL)):
            response, release = self.probe(respond, directory)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual('validate', body['step'])
        self.assertFalse(body['ok'])
        self.assertNotIn(SECRET, response.text)
        self.assertEqual('The base URL is not valid for this LLM mode. '
                         'Enter a valid endpoint URL and try again.', body['error'])
        release.assert_called_once_with('llm_test', 'owned')

    def test_success_and_model_picker_are_unaffected(self):
        def respond(request):
            if request.url.path.endswith('/models'):
                return httpx2.Response(200, json=MODELS)
            return httpx2.Response(200, json=REPLY)

        def make_sdk(**kwargs):
            return OpenAI(**kwargs, http_client=transport)

        with httpx2.Client(transport=httpx2.MockTransport(respond)) as transport, \
                patch.object(llm_provider, 'OpenAI', side_effect=make_sdk), \
                patch.object(system, 'claim_gpu_task', return_value='owned'), \
                patch.object(system, 'release_gpu_task_claim') as release, \
                TestClient(self.app) as client:
            probe = client.post('/api/llm/test', json=PROFILE)
            picker = client.post('/api/llm/models', json=PROFILE)
        self.assertEqual(200, probe.status_code, probe.text)
        result = probe.json()
        self.assertTrue(result['ok'])
        self.assertEqual('pong', result['reply'])
        self.assertEqual(['chosen'], result['models'])
        self.assertTrue(result['model_present'])
        release.assert_called_once_with('llm_test', 'owned')
        self.assertEqual(200, picker.status_code, picker.text)
        self.assertEqual({'models': ['chosen']}, picker.json())


if __name__ == '__main__':
    unittest.main()
