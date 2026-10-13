"""Label review cannot change text, boundaries, policy or private metadata."""
import copy
import unittest
from unittest.mock import patch
from generate_script import LLMGenParams
from review_script import review_batch, get_review_label_binding
from tests.test_pass_boundary_retry_regressions import client_for


class ReviewLabelsTests(unittest.TestCase):
    batch = [{'speaker': 'BOB', 'text': 'And yet— Just turn your head.',
              'instruct': '', 'metadata': {'source': [4, 9]}},
             {'speaker': 'NARRATOR', 'text': 'said Alice.', 'instruct': 'Neutral.'}]
    labels = [{'n': 0, 'speaker': 'ALICE', 'instruct': 'Gently.'},
              {'n': 1, 'speaker': 'NARRATOR', 'instruct': 'Neutral.'}]

    def test_corrects_speaker_without_reemitting_text_or_losing_metadata(self):
        original = copy.deepcopy(self.batch)
        client, calls = client_for([(self.labels[::-1], 'stop')])
        out = review_batch(client, 'm', self.batch, 1, 1,
                           LLMGenParams(review_labels_only=True))
        self.assertEqual('ALICE', out[0]['speaker'])
        self.assertEqual(original, self.batch)
        self.assertEqual([e['text'] for e in original], [e['text'] for e in out])
        self.assertEqual(original[0]['metadata'], out[0]['metadata'])
        self.assertIsNot(self.batch[0]['metadata'], out[0]['metadata'])
        self.assertIn('And yet—', calls[0]['messages'][-1]['content'])
        self.assertIn('Never return text', calls[0]['messages'][0]['content'])

    def test_labels_mode_does_not_format_custom_full_text_template(self):
        client, _ = client_for([(self.labels, 'stop')])
        out = review_batch(client, 'm', self.batch, 1, 1,
            LLMGenParams(review_labels_only=True, user_prompt_template='{unsupported_full_text_placeholder}'))
        self.assertEqual('ALICE', out[0]['speaker'])

    def test_rejects_invented_text_incomplete_or_duplicate_indexes(self):
        for rows in ([{**self.labels[0], 'text': 'And yet,'}, self.labels[1]],
                     self.labels[:1], [self.labels[0]] * 2,
                     [{**self.labels[0], 'n': True}, self.labels[1]],
                     [{**self.labels[0], 'speaker': ''}, self.labels[1]],
                     [{**self.labels[0], 'instruct': None}, self.labels[1]]):
            with self.subTest(rows=rows):
                self.assertFalse(get_review_label_binding(self.batch, rows)[1]['passed'])

    def test_bad_labels_retry_and_bind_corrected_response(self):
        client, calls = client_for([(self.labels[:1], 'stop'), (self.labels, 'stop')])
        with patch('generate_script.time.sleep'):
            out = review_batch(client, 'm', self.batch, 1, 1,
                               LLMGenParams(review_labels_only=True), max_retries=1)
        self.assertEqual(2, len(calls))
        self.assertEqual('ALICE', out[0]['speaker'])

    def test_exhaustion_returns_none_without_mutating_original(self):
        client, calls = client_for([(self.labels[:1], 'stop')])
        original = copy.deepcopy(self.batch)
        self.assertIsNone(review_batch(client, 'm', self.batch, 1, 1,
            LLMGenParams(review_labels_only=True), max_retries=0))
        self.assertEqual(original, self.batch)

    def test_full_text_review_remains_available_for_lossless_boundary_changes(self):
        original = [{'speaker': 'BOB', 'text': 'Hello. said Alice.', 'instruct': ''}]
        answer = [{'speaker': 'ALICE', 'text': 'Hello.', 'instruct': ''},
                  {'speaker': 'NARRATOR', 'text': 'said Alice.', 'instruct': ''}]
        client, _ = client_for([(answer, 'stop')])
        self.assertEqual(answer, review_batch(client, 'm', original, 1, 1, LLMGenParams()))

    def test_exact_schema_limits_review_count_and_indexes(self):
        from review_script import REVIEW_LABEL_SCHEMA
        original = copy.deepcopy(REVIEW_LABEL_SCHEMA)
        client, calls = client_for([(self.labels, 'stop')])
        review_batch(client, 'm', self.batch, 1, 1, LLMGenParams(review_labels_only=True))
        schema = calls[0]['response_format']['json_schema']['schema']
        self.assertEqual(2, schema['minItems'])
        self.assertEqual(2, schema['maxItems'])
        self.assertEqual([0, 1], schema['items']['properties']['n']['enum'])
        self.assertEqual(original, REVIEW_LABEL_SCHEMA)

    def test_narrator_character_shift_retries_before_binding(self):
        bad = [self.labels[0], {**self.labels[1], 'speaker': 'ALICE'}]
        report = get_review_label_binding(self.batch, bad)[1]
        self.assertEqual('narrator_renamed', report['findings'][0]['code'])
        client, calls = client_for([(bad, 'stop'), (self.labels, 'stop')])
        with patch('generate_script.time.sleep'):
            out = review_batch(client, 'm', self.batch, 1, 1,
                               LLMGenParams(review_labels_only=True), max_retries=1)
        self.assertEqual(2, len(calls))
        self.assertEqual('NARRATOR', out[1]['speaker'])

    def test_explicit_spoken_type_can_correct_a_wrong_narrator_label(self):
        batch = [{**self.batch[0], 'type': 'SPOKEN', 'speaker': 'NARRATOR'}, self.batch[1]]
        self.assertTrue(get_review_label_binding(batch, self.labels)[1]['passed'])
        batch[0]['type'] = 'NARRATOR'
        self.assertFalse(get_review_label_binding(batch, self.labels)[1]['passed'])
