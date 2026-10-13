"""Synthetic requests through the real SDK pin task and window allowances."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
import generate_script as gs
from llm_provider import ConfiguredOpenAI, FailoverClient
import three_pass_generate as tp


class DispatchLimitsTests(unittest.TestCase):
    def client(self, content, lengths=()):
        sent = []

        def respond(request):
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={
                'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                'model': body['model'], 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': content},
                    'finish_reason': 'length' if len(sent) - 1 in lengths else 'stop'}]})

        sdk = OpenAI(base_url='http://fixture.invalid/v1', api_key='synthetic',
                     max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(sdk.close)
        return ConfiguredOpenAI(sdk, {}), sent

    def quiet_logs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return patch.object(gs, 'get_response_log_path',
                            side_effect=lambda name: str(Path(tmp.name) / name))

    def test_blank_profile_limits_cannot_override_runtime_task_budget(self):
        for persona in (False, True):
            for key in ('max_tokens', 'max_completion_tokens'):
                with self.subTest(persona=persona, key=key):
                    content = '{"description":"Calm.","ref_text":"Hello."}' if persona else '[{"speaker":"NARRATOR","text":"Hello.","instruct":""}]'
                    client, sent = self.client(content)
                    params = gs.LLMGenParams(max_tokens=128, hard_max_tokens=128,
                        context_length=1024, provider_extra_body={key: 20000})
                    with self.quiet_logs(), contextlib.redirect_stdout(io.StringIO()):
                        if persona:
                            result = gs.call_llm_for_object(client, 'fixture', 's', 'Hello.', params,
                                                           label='PERSONA', max_retries=0)
                        else:
                            result = gs.call_llm_for_entries(client, 'fixture', 's', 'Hello.', params,
                                                            'fixture.log', 'GEN', max_retries=0)
                    self.assertTrue(result)
                    self.assertEqual(128, sent[0]['max_tokens'])
                    self.assertNotIn('max_completion_tokens', sent[0])

    def test_body_messages_cannot_replace_runtime_checked_input(self):
        content = '[{"speaker":"NARRATOR","text":"Hello.","instruct":""}]'
        client, sent = self.client(content)
        params = gs.LLMGenParams(max_tokens=128, hard_max_tokens=128,
            context_length=1024, provider_extra_body={
                'messages': [{'role': 'user', 'content': 'oversized ' * 1000}]})
        with self.quiet_logs(), contextlib.redirect_stdout(io.StringIO()):
            result = gs.call_llm_for_entries(client, 'fixture', 's', 'Hello.', params,
                                            'fixture.log', 'GEN', max_retries=0)
        self.assertTrue(result)
        self.assertEqual([{'role': 'system', 'content': 's'},
                          {'role': 'user', 'content': 'Hello.'}], sent[0]['messages'])

    def test_provider_defaults_lose_to_request_and_reasoning_body_alias_survives(self):
        client, sent = self.client('{}')
        client = ConfiguredOpenAI(client._client, {'max_completion_tokens': 32, 'option': 'default'})
        client.chat.completions.create(model='gpt-5', messages=[], max_tokens=128,
                                      extra_body={'option': 'request'})
        self.assertEqual(128, sent[0]['max_completion_tokens'])
        self.assertNotIn('max_tokens', sent[0])
        self.assertEqual('request', sent[0]['option'])
        client.chat.completions.create(model='gpt-5', messages=[],
                                      extra_body={'max_completion_tokens': 64})
        self.assertEqual(64, sent[1]['max_completion_tokens'])
        self.assertNotIn('max_tokens', sent[1])

    def test_native_small_windows_keep_initial_budget_after_failover_and_retry(self):
        for instruct in (False, True):
            for switched in (False, True):
                with self.subTest(instruct=instruct, switched=switched):
                    content = json.dumps([{'n': 0, 'speaker': 'PIP', 'instruct': 'Calm.'}])
                    configured, sent = self.client(content, lengths=(0,))
                    client = FailoverClient(configured, 'primary', configured, 'secondary',
                        secondary_config={'model_name': 'secondary', 'base_url': 'http://fixture.invalid/v1'})
                    if switched:
                        client.failover({'category': 'timeout'})
                    params = gs.LLMGenParams(max_tokens=10000, task_max_tokens=10000,
                        hard_max_tokens=16384, task_hard_max_tokens=16384,
                        context_length=32768, observed_calls=1, temperature=0)
                    with self.quiet_logs(), patch('lmstudio_settings.get_current_status', return_value={
                            'available': True, 'loaded': True, 'context_length': 32768, 'parallel': 1}), \
                            contextlib.redirect_stdout(io.StringIO()):
                        if instruct:
                            result = tp.instruct_batch(client, 'primary',
                                [{'speaker': 'PIP', 'text': 'Wait for me!'}], params, max_retries=2)
                        else:
                            result = tp.attribute_batch(client, 'primary',
                                [{'type': 'SPOKEN', 'text': 'Wait for me!'}], params, roster=[], max_retries=2)
                    self.assertTrue(result)
                    self.assertEqual(1024, sent[0]['max_tokens'])
                    self.assertGreater(sent[1]['max_tokens'], 1024)
                    self.assertTrue(all(body['max_tokens'] <= 16384 for body in sent))
                    self.assertEqual(10000, params.max_tokens)
