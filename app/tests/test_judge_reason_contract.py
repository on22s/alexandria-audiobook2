"""Judge evidence is validated on the real request/retry and JSONL paths."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import attribution_prompt_variants as apv
import three_pass_generate as tp
from generate_script import LLMGenParams

if os.environ.get('JUDGE_SOURCE'):
    spec = importlib.util.spec_from_file_location('judge_saved', os.environ['JUDGE_SOURCE'])
    apv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apv)

FROZEN = [{'type': 'NARRATOR', 'text': 'Alice said to Bob.'},
          {'type': 'SPOKEN', 'text': 'Come here.'}]
GOOD = [{'n': 1, 'speaker': 'ALICE', 'why': 'Alice said to Bob names Alice as the speaker.'},
        {'n': 0, 'speaker': 'NARRATOR'}]


class Client:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []
        self.base_url = 'http://localhost:1234/v1'
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(next(self.answers))), finish_reason='stop')], usage=None)


class JudgeReasonContractTests(unittest.TestCase):
    def test_missing_empty_or_wrong_type_reason_cannot_be_accepted_or_append(self):
        for why in [None, '', '  ', 4, [], {}]:
            with self.subTest(why=why), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'judge.jsonl'
                path.write_text('{"prior":"evidence"}\n')
                before = path.read_bytes()
                answer = copy.deepcopy(GOOD)
                answer[0]['why'] = why
                client = Client([answer])
                with patch.dict(os.environ, {'JUDGE_WHY_PATH': str(path)}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(tp.PassExhausted):
                        tp.attribute_batch(client, 'm', FROZEN, LLMGenParams(structured_output='off'),
                                           ['ALICE', 'BOB'], max_retries=0,
                                           entries_provider=apv.make_provider('judge'))
                    apv.record_judge_reasons(FROZEN, answer)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(1, len(client.calls))

    def test_rejection_retries_real_validator_then_records_only_accepted_evidence(self):
        bad = [{k: v for k, v in entry.items() if k != 'why'} for entry in GOOD]
        client = Client([bad, GOOD])
        attempts = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'judge.jsonl'
            with patch.dict(os.environ, {'JUDGE_WHY_PATH': str(path)}), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = tp.attribute_batch(client, 'm', FROZEN,
                    LLMGenParams(structured_output='off'), ['ALICE', 'BOB'], max_retries=1,
                    entries_provider=apv.make_provider('judge'), attempt_observer=attempts.append)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(32, len(rows[0]['run_id']))
            self.assertEqual([{'text': 'Come here.', 'speaker': 'ALICE', 'why': GOOD[0]['why']}],
                             [{k: row[k] for k in ('text', 'speaker', 'why')} for row in rows])
        self.assertEqual(2, len(client.calls))
        self.assertIn('missing_judge_reason', attempts[0]['failure_codes'])
        self.assertIn('nonempty why', client.calls[1]['messages'][-1]['content'])
        self.assertEqual(['Alice said to Bob.', 'Come here.'], [row['text'] for row in result])
        self.assertEqual(['NARRATOR', 'ALICE'], [row['speaker'] for row in result])

    def test_structured_request_can_express_reasons_without_changing_canonical_schema(self):
        original = copy.deepcopy(tp.ATTRIBUTION_RESPONSE_SCHEMA)
        params = LLMGenParams(structured_output='auto', response_schema=tp.ATTRIBUTION_RESPONSE_SCHEMA)
        client = Client([GOOD])
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {'JUDGE_WHY_PATH': ''}):
            tp.attribute_batch(client, 'm', FROZEN, params, ['ALICE', 'BOB'], max_retries=0,
                               entries_provider=apv.make_provider('judge'))
        schema = client.calls[0]['response_format']['json_schema']['schema']
        branches = []
        for index, item in enumerate(schema['items']):
            self.assertTrue(all(branch['properties']['n']['enum'] == [index]
                                for branch in item['anyOf']))
            branches.extend(item['anyOf'])
        self.assertFalse(schema['additionalItems'])
        self.assertEqual(4, len(branches))
        self.assertEqual((2, 2), (schema['minItems'], schema['maxItems']))
        for branch in branches:
            fields = {'n', 'speaker'}
            if 'why' in branch['properties']:
                fields.add('why')
                self.assertEqual({'type': 'string'}, branch['properties']['why'])
            self.assertEqual(fields, set(branch['required']))
            self.assertEqual(fields, set(branch['properties']))
            self.assertFalse(branch['additionalProperties'])
            indexes = branch['properties']['n']['enum']
            self.assertIn(indexes, ([0], [1]))
            self.assertEqual(tp.SPEAKER_MAX_CHARS, branch['properties']['speaker']['maxLength'])
            if indexes == [0]:
                self.assertEqual('NARRATOR', branch['properties']['speaker']['const'])
            else:
                self.assertNotIn('const', branch['properties']['speaker'])
        self.assertEqual(
            [((0,), False), ((0,), True), ((1,), False), ((1,), True)],
            sorted((tuple(branch['properties']['n']['enum']),
                    'why' in branch['properties']) for branch in branches))
        self.assertEqual(original, tp.ATTRIBUTION_RESPONSE_SCHEMA)
        self.assertIs(params.response_schema, tp.ATTRIBUTION_RESPONSE_SCHEMA)

    def test_existing_speaker_gate_still_rejects_even_with_evidence(self):
        invalid = copy.deepcopy(GOOD)
        invalid[0]['speaker'] = 'NARRATOR'
        client = Client([invalid])
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {'JUDGE_WHY_PATH': ''}):
            with self.assertRaises(tp.PassExhausted):
                tp.attribute_batch(client, 'm', FROZEN, LLMGenParams(structured_output='off'),
                                   ['ALICE', 'BOB'], max_retries=0,
                                   entries_provider=apv.make_provider('judge'))
