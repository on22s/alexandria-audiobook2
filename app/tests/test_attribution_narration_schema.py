"""Source narration roles constrain generation without replacing validation."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import attribution_prompt_variants as prompts
from generate_script import LLMGenParams
import three_pass_generate as tp
from tests.test_pass_boundary_retry_regressions import client_for


class NarrationSchemaTests(unittest.TestCase):
    batch = [{'type': 'SPOKEN', 'text': 'I will go.'},
             {'type': 'NARRATOR', 'text': 'said the lad.'},
             {'type': 'SPOKEN', 'text': 'So will I.'}]
    answer = [{'n': 0, 'speaker': 'ALICE'}, {'n': 1, 'speaker': 'NARRATOR'},
              {'n': 2, 'speaker': 'BOB'}]

    def get_items(self, schema):
        item = schema['items']
        positions = item if isinstance(item, list) else [item]
        return [alternative for position in positions
                for alternative in position.get('anyOf', [position])]

    def get_request(self, variant=None, *, traits=False, params=None, responses=None):
        client, calls = client_for(responses or [(deepcopy(self.answer), 'stop')])
        result = tp.attribute_batch(client, 'model', deepcopy(self.batch),
            params or LLMGenParams(max_tokens=500), ['ALICE', 'BOB'],
            max_retries=1, speaker_traits=traits,
            entries_provider=prompts.make_provider(variant) if variant else None,
            surround={'entries': [{**row, 'n': i} for i, row in enumerate(self.batch)]})
        return result, calls

    def test_native_request_binds_narration_indexes_without_a_name_whitelist(self):
        result, calls = self.get_request()
        schema = calls[0]['response_format']['json_schema']['schema']
        self.assertEqual((3, 3), (schema['minItems'], schema['maxItems']))
        self.assertIsInstance(schema['items'], list)
        spoken, narration, last = self.get_items(schema)
        self.assertEqual([0], spoken['properties']['n']['enum'])
        self.assertEqual([2], last['properties']['n']['enum'])
        self.assertEqual([1], narration['properties']['n']['enum'])
        self.assertEqual('NARRATOR', narration['properties']['speaker']['const'])
        self.assertNotIn('enum', spoken['properties']['speaker'])
        self.assertNotIn('const', spoken['properties']['speaker'])
        self.assertEqual(tp.SPEAKER_MAX_CHARS, spoken['properties']['speaker']['maxLength'])
        for item in (spoken, narration, last):
            self.assertFalse(item['additionalProperties'])
            self.assertEqual(['n', 'speaker'], item['required'])
        self.assertEqual(['ALICE', 'NARRATOR', 'BOB'], [row['speaker'] for row in result])
        self.assertEqual([row['text'] for row in self.batch], [row['text'] for row in result])

    def test_all_builtin_requests_retain_their_fields_and_target_indexes(self):
        for variant in prompts.VARIANTS:
            with self.subTest(variant=variant):
                answer = deepcopy(self.answer)
                if variant == 'michel2_spoken':
                    answer = [answer[0], answer[2]]
                if variant == 'judge':
                    answer[0]['why'] = 'The nearby context identifies Alice.'
                    answer[2]['why'] = 'Bob answers.'
                with patch.object(prompts, 'rolling_summary', return_value=''):
                    _, calls = self.get_request(variant, responses=[(answer, 'stop')])
                schema = calls[0]['response_format']['json_schema']['schema']
                self.assertEqual(2 if variant == 'michel2_spoken' else 3, schema['maxItems'])
                targets = set()
                for item in self.get_items(schema):
                    targets.update(item['properties']['n']['enum'])
                    if 1 in item['properties']['n']['enum']:
                        self.assertEqual('NARRATOR', item['properties']['speaker']['const'])
                self.assertEqual({0, 2} if variant == 'michel2_spoken' else {0, 1, 2}, targets)
                if variant == 'judge':
                    self.assertTrue(any('why' in item['required'] for item in self.get_items(schema)))

    def test_trait_fields_survive_on_both_source_roles(self):
        _, calls = self.get_request('michel2_full', traits=True)
        for item in self.get_items(calls[0]['response_format']['json_schema']['schema']):
            self.assertTrue({'gender', 'age_group', 'ageless'} <= set(item['properties']))

    def test_schema_and_frozen_inputs_are_not_mutated(self):
        original = deepcopy(tp.ATTRIBUTION_RESPONSE_SCHEMA)
        frozen = deepcopy(self.batch)
        schema = tp.get_attribution_response_schema(True, range(3))
        snapshot = deepcopy(schema)
        narrowed = tp.get_exact_index_schema(schema, range(3), frozen_entries=frozen)
        narrowed['schema']['items']['anyOf'][1]['properties']['speaker']['const'] = 'CHANGED'
        self.assertEqual(snapshot, schema)
        self.assertEqual(original, tp.ATTRIBUTION_RESPONSE_SCHEMA)
        self.assertEqual(self.batch, frozen)

    def test_single_role_and_sparse_renarrowing_have_no_empty_alternatives(self):
        schema = tp.get_attribution_response_schema(indexes=range(3))
        narrowed = tp.get_exact_index_schema(schema, range(3), frozen_entries=self.batch)
        sparse = tp.get_exact_index_schema(narrowed, [0, 2])
        self.assertNotIn('anyOf', sparse['schema']['items'])
        self.assertEqual([0, 2], sparse['schema']['items']['properties']['n']['enum'])
        narration = tp.get_exact_index_schema(schema, [1], frozen_entries=self.batch)
        self.assertNotIn('anyOf', narration['schema']['items'])
        self.assertEqual('NARRATOR', narration['schema']['items']['properties']['speaker']['const'])
        empty = tp.get_exact_index_schema(narrowed, [], frozen_entries=self.batch)
        self.assertEqual((0, 0), (empty['schema']['minItems'], empty['schema']['maxItems']))
        self.assertTrue(empty['schema']['items']['anyOf'])

    def test_ignored_schema_keeps_native_refusal_and_existing_retry_budget(self):
        shifted = [{'n': 0, 'speaker': 'NARRATOR'}, {'n': 1, 'speaker': 'ALICE'},
                   {'n': 2, 'speaker': 'BOB'}]
        client, calls = client_for([(deepcopy(shifted), 'stop')] * 2)
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, 'model', self.batch, LLMGenParams(max_tokens=500),
                ['ALICE', 'BOB'], max_retries=1)
        self.assertEqual(2, len(calls))
        self.assertEqual('ALICE', shifted[1]['speaker'])

    def test_structured_off_keeps_native_validation_without_sending_a_schema(self):
        _, calls = self.get_request(params=LLMGenParams(max_tokens=500, structured_output='off'))
        self.assertNotIn('response_format', calls[0])

    def test_policy_revision_invalidates_checkpoint_identity_including_freeform_failover(self):
        for mode in ('auto', 'off'):
            params = LLMGenParams(structured_output=mode)
            first = tp.three_pass_fingerprint('source', 'model', 3000, params)
            with patch.object(tp, 'ATTRIBUTION_SCHEMA_POLICY', 'previous_policy'):
                second = tp.three_pass_fingerprint('source', 'model', 3000, params)
            with self.subTest(mode=mode):
                self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
