"""Positional output negotiation preserves native attribution safety."""
from contextlib import nullcontext
from copy import deepcopy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

import generate_script as gs
from llm_provider import FailoverClient
import three_pass_generate as tp


class OrderedIndexSchemaTests(unittest.TestCase):
    frozen = [{'type': 'SPOKEN', 'text': 'I will go.', 'uid': 'first'},
              {'type': 'NARRATOR', 'text': 'Alice said.', 'uid': 'tag'},
              {'type': 'SPOKEN', 'text': 'So will I.', 'uid': 'last'}]
    answer = [{'n': 0, 'speaker': 'ALICE'}, {'n': 1, 'speaker': 'NARRATOR'},
              {'n': 2, 'speaker': 'BOB'}]
    tuple_error = 'Invalid schema for response_format: items must be an object or boolean, got array'

    def get_client(self, handler, url='http://fixture/v1'):
        client = OpenAI(api_key='fixture', base_url=url, max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.addCleanup(client.close)
        return client

    def get_reply(self, rows=None):
        return httpx2.Response(200, json={'id': 'fixture-result', 'object': 'chat.completion',
            'created': 0, 'model': 'fixture', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': json.dumps(
                    self.answer if rows is None else rows)}, 'finish_reason': 'stop'}]})

    def get_error(self, status, message):
        return httpx2.Response(status, json={'error': {'message': message, 'type': 'invalid_request_error'}})

    def get_params(self):
        return gs.LLMGenParams(max_tokens=500, hard_max_tokens=1000, temperature=.3,
                               api_retry_limit=0)

    def run_batch(self, client, params, retries=0, attempts=None, *, positional=False):
        # Negotiation fixtures explicitly opt in; default fixtures exercise the
        # unpatched production selector on every native retry.
        selector = gs.get_response_format
        explicit_positional = (patch.object(gs, 'get_response_format',
            side_effect=lambda params, client, *, portable=False: selector(
                params, client, portable=portable)) if positional else nullcontext())
        with explicit_positional, tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: os.path.join(tmp, name)):
            return tp.attribute_batch(client, 'fixture', deepcopy(self.frozen), params,
                ['ALICE', 'BOB'], max_retries=retries,
                attempt_observer=attempts.append if attempts is not None else None)

    def test_production_default_sends_portable_schema_and_preserves_frozen_source(self):
        sent = []
        frozen = deepcopy(self.frozen)
        def respond(request):
            sent.append(json.loads(request.content))
            return self.get_reply()
        result = self.run_batch(self.get_client(respond), self.get_params())
        self.assertEqual(1, len(sent))
        schema = sent[0]['response_format']['json_schema']['schema']
        self.assertIsInstance(schema['items'], dict)
        self.assertNotIn('additionalItems', schema)
        self.assertEqual((3, 3), (schema['minItems'], schema['maxItems']))
        spoken, narrator = schema['items']['anyOf']
        self.assertEqual([0, 2], spoken['properties']['n']['enum'])
        self.assertEqual([1], narrator['properties']['n']['enum'])
        self.assertEqual('NARRATOR', narrator['properties']['speaker']['const'])
        self.assertEqual(frozen, self.frozen)
        self.assertEqual([{**{key: value for key, value in row.items() if key != 'type'},
                          'speaker': label} for row, label in zip(
            frozen, ['ALICE', 'NARRATOR', 'BOB'])], result)

    def test_production_alignment_retry_keeps_portable_schema_and_native_budget(self):
        sent, attempts = [], []
        duplicate = [self.answer[0], self.answer[1], self.answer[0]]
        def respond(request):
            sent.append(json.loads(request.content))
            return self.get_reply(duplicate if len(sent) == 1 else None)
        params = self.get_params()
        result = self.run_batch(self.get_client(respond), params,
                                retries=1, attempts=attempts)
        self.assertEqual(2, len(sent))
        self.assertEqual(['quality_rejected', 'accepted'], [a['outcome'] for a in attempts])
        self.assertEqual(['alignment_violated'], attempts[0]['failure_codes'])
        self.assertEqual(sent[0]['response_format'], sent[1]['response_format'])
        self.assertTrue(all(isinstance(body['response_format']['json_schema']['schema']['items'],
                                      dict) for body in sent))
        self.assertEqual(sent[0]['max_tokens'], sent[1]['max_tokens'])
        self.assertEqual((500, 1000), (params.max_tokens, params.hard_max_tokens))
        self.assertFalse(params.schema_rejected_by | params.ordered_schema_rejected_by)
        self.assertEqual(['ALICE', 'NARRATOR', 'BOB'], [row['speaker'] for row in result])

    def test_explicit_positional_sdk_wire_binds_each_position_and_preserves_source(self):
        sent = []
        def respond(request):
            sent.append(json.loads(request.content))
            return self.get_reply()
        result = self.run_batch(self.get_client(respond), self.get_params(), positional=True)
        schema = sent[0]['response_format']['json_schema']['schema']
        self.assertEqual([[0], [1], [2]], [row['properties']['n']['enum'] for row in schema['items']])
        self.assertFalse(schema['additionalItems'])
        self.assertEqual('NARRATOR', schema['items'][1]['properties']['speaker']['const'])
        self.assertEqual([row['text'] for row in self.frozen], [row['text'] for row in result])
        self.assertEqual([row['uid'] for row in self.frozen], [row['uid'] for row in result])
        self.assertEqual(['ALICE', 'NARRATOR', 'BOB'], [row['speaker'] for row in result])

    def test_tuple_rejection_keeps_portable_schema_and_sticks_only_for_endpoint_and_run(self):
        sent = []
        def respond(request):
            body = json.loads(request.content); sent.append(body)
            if isinstance(body['response_format']['json_schema']['schema']['items'], list):
                return self.get_error(400, self.tuple_error)
            return self.get_reply()
        client, params = self.get_client(respond), self.get_params()
        self.run_batch(client, params, positional=True)
        self.run_batch(client, params, positional=True)
        self.assertEqual(3, len(sent))
        self.assertEqual(sent[0]['messages'], sent[1]['messages'])
        self.assertEqual(sent[0]['max_tokens'], sent[1]['max_tokens'])
        portable = sent[1]['response_format']['json_schema']['schema']
        self.assertIsInstance(portable['items'], dict)
        self.assertEqual(portable, sent[2]['response_format']['json_schema']['schema'])
        narrator = portable['items']['anyOf'][1]
        self.assertEqual([1], narrator['properties']['n']['enum'])
        self.assertEqual('NARRATOR', narrator['properties']['speaker']['const'])
        self.assertFalse(params.schema_rejected_by)
        self.assertEqual({'http://fixture/v1/'}, params.ordered_schema_rejected_by)
        self.run_batch(client, self.get_params(), positional=True)
        self.assertIsInstance(sent[3]['response_format']['json_schema']['schema']['items'], list)
        other = []
        def accept(request):
            other.append(json.loads(request.content)); return self.get_reply()
        self.run_batch(self.get_client(accept, 'http://other/v1'), params, positional=True)
        self.assertIsInstance(other[0]['response_format']['json_schema']['schema']['items'], list)

    def test_whole_feature_rejection_retains_existing_single_fallback(self):
        for message in ['response_format is not supported',
                        'Invalid schema for response_format: response_format type must be one of text or json_object, but got json_schema',
                        "Invalid schema: response_format.type must be 'text' or 'json_object'",
                        'Invalid schema: response_format is unsupported']:
            with self.subTest(message=message):
                sent = []
                def respond(request):
                    body = json.loads(request.content); sent.append(body)
                    return (self.get_error(400, message)
                            if 'response_format' in body else self.get_reply())
                params = self.get_params(); client = self.get_client(respond)
                self.run_batch(client, params, positional=True); self.run_batch(client, params, positional=True)
                self.assertEqual(3, len(sent))
                self.assertIn('response_format', sent[0])
                self.assertNotIn('response_format', sent[1])
                self.assertNotIn('response_format', sent[2])
                self.assertFalse(params.ordered_schema_rejected_by)
                self.assertEqual({'http://fixture/v1/'}, params.schema_rejected_by)

    def test_cached_portable_schema_can_later_record_whole_feature_rejection(self):
        sent = []
        def respond(request):
            body = json.loads(request.content); sent.append(body)
            if len(sent) == 1:
                return self.get_error(422, self.tuple_error)
            if len(sent) == 3:
                return self.get_error(400, 'response_format is not supported')
            return self.get_reply()
        params = self.get_params(); client = self.get_client(respond)
        self.run_batch(client, params, positional=True); self.run_batch(client, params, positional=True)
        self.assertEqual(4, len(sent))
        self.assertIsInstance(sent[2]['response_format']['json_schema']['schema']['items'], dict)
        self.assertNotIn('response_format', sent[3])
        self.assertEqual({'http://fixture/v1/'}, params.schema_rejected_by)

    def test_tuple_error_policy_requires_explicit_schema_shape_evidence(self):
        for message in [self.tuple_error,
                        'JSON schema error: tuple validation is not supported',
                        'Invalid schema: use prefixItems for tuple validation']:
            for status in (400, 422):
                error = type('APIError', (Exception,), {'status_code': status})(message)
                self.assertTrue(gs.is_ordered_schema_rejection(error))
        for status, message in [(500, self.tuple_error), (401, self.tuple_error),
                                (400, 'Invalid schema for response_format: unknown unsupported keyword'),
                                (422, 'json_schema has an unsupported pattern keyword'),
                                (400, 'context length exceeded')]:
            error = type('APIError', (Exception,), {'status_code': status})(message)
            self.assertFalse(gs.is_ordered_schema_rejection(error))

    def test_portable_errors_are_not_reinterpreted_as_capability_rejection(self):
        for status, message in [(400, 'Invalid schema for response_format: unsupported keyword mystery'),
                                (400, 'json_schema has an unsupported pattern keyword'),
                                (400, 'context length exceeded'), (401, 'unauthorized'),
                                (429, 'quota exhausted'), (500, 'json_schema grammar failed')]:
            with self.subTest(status=status, message=message):
                sent = []
                def respond(request):
                    body = json.loads(request.content); sent.append(body)
                    return self.get_error(400, self.tuple_error) if len(sent) == 1 else self.get_error(status, message)
                params = self.get_params()
                with self.assertRaises(tp.PassExhausted):
                    self.run_batch(self.get_client(respond), params, positional=True)
                self.assertEqual(2, len(sent))
                self.assertTrue(all('response_format' in row for row in sent))
                self.assertFalse(params.schema_rejected_by)

    def test_non_tuple_failure_never_downgrades_the_ordered_schema(self):
        for status, message in [(400, 'Invalid schema for response_format: unknown unsupported keyword'),
                                (400, 'json_schema has an unsupported pattern keyword'),
                                (400, 'context length exceeded'), (401, 'unauthorized'),
                                (429, 'quota exhausted'), (500, 'tuple schema unsupported')]:
            with self.subTest(status=status):
                sent = []
                def respond(request):
                    sent.append(json.loads(request.content)); return self.get_error(status, message)
                params = self.get_params()
                with self.assertRaises(tp.PassExhausted):
                    self.run_batch(self.get_client(respond), params, positional=True)
                self.assertEqual(1, len(sent))
                self.assertFalse(params.schema_rejected_by | params.ordered_schema_rejected_by)

    def test_ignored_schema_still_rejects_duplicate_indices_without_repair(self):
        sent, attempts = [], []
        duplicate = [self.answer[0], self.answer[1], self.answer[0]]
        def respond(request):
            sent.append(json.loads(request.content)); return self.get_reply(duplicate)
        with self.assertRaises(tp.PassExhausted):
            self.run_batch(self.get_client(respond), self.get_params(), retries=1, attempts=attempts)
        self.assertEqual(2, len(sent))
        self.assertEqual(['quality_rejected', 'quality_rejected'], [a['outcome'] for a in attempts])
        self.assertTrue(all(a['failure_codes'] == ['alignment_violated'] for a in attempts))

    def test_sparse_and_empty_renarrowing_preserve_original_schemas(self):
        original = deepcopy(tp.ATTRIBUTION_RESPONSE_SCHEMA)
        schema = tp.get_exact_index_schema(tp.ATTRIBUTION_RESPONSE_SCHEMA, range(3), frozen_entries=self.frozen)
        snapshot = deepcopy(schema)
        sparse = tp.get_exact_index_schema(schema, [0, 2])
        self.assertEqual([[0], [2]], [item['properties']['n']['enum'] for item in sparse['ordered_schema']['items']])
        empty = tp.get_exact_index_schema(schema, [])
        self.assertNotIn('ordered_schema', empty)
        self.assertEqual(0, empty['schema']['maxItems'])
        self.assertEqual(snapshot, schema)
        self.assertEqual(original, tp.ATTRIBUTION_RESPONSE_SCHEMA)

    def test_profile_switch_does_not_inherit_the_primary_tuple_rejection(self):
        first, second = [], []
        def primary(request):
            body = json.loads(request.content); first.append(body)
            if isinstance(body['response_format']['json_schema']['schema']['items'], list):
                return self.get_error(400, self.tuple_error)
            return self.get_reply()
        def secondary(request):
            second.append(json.loads(request.content)); return self.get_reply()
        client = FailoverClient(self.get_client(primary), 'primary',
            self.get_client(secondary, 'http://secondary/v1'), 'secondary', secondary_label='local',
            secondary_config={'base_url': 'http://secondary/v1', 'model_name': 'secondary',
                              'structured_output': 'auto', 'api_retry_limit': 0})
        params = self.get_params()
        self.run_batch(client, params, positional=True)
        client.failover({'category': 'timeout'})
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1}):
            self.run_batch(client, params, positional=True)
        self.assertEqual(2, len(first))
        self.assertEqual(1, len(second))
        self.assertIsInstance(second[0]['response_format']['json_schema']['schema']['items'], list)
        self.assertEqual('secondary', second[0]['model'])
        self.assertEqual(first[0]['max_tokens'], second[0]['max_tokens'])

    def test_portable_fallback_still_passes_secondary_request_admission(self):
        sent, admitted = [], []
        def respond(request):
            sent.append(json.loads(request.content)); return self.get_error(400, self.tuple_error)
        sdk = self.get_client(respond, 'http://secondary/v1')
        client = FailoverClient(sdk, 'primary', sdk, 'secondary', secondary_label='local',
            secondary_config={'base_url': 'http://secondary/v1', 'model_name': 'secondary',
                              'structured_output': 'auto', 'api_retry_limit': 0})
        client.failover({'category': 'timeout'})
        params = self.get_params()
        def admission(profile):
            admitted.append(profile)
            return len(admitted) == 1
        params.request_admission = admission
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1}):
            with self.assertRaises(tp.PassExhausted):
                self.run_batch(client, params, positional=True)
        self.assertEqual(2, len(admitted))
        self.assertEqual(1, len(sent), 'Admission refusal must prevent the portable wire send')


if __name__ == '__main__':
    unittest.main()
