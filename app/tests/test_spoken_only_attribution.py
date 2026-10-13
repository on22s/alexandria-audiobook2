"""Sparse spoken indexes must bind to the original full batch."""
import unittest
from attribution_prompt_variants import make_provider, get_expanded_spoken_answer
from generate_script import LLMGenParams
import three_pass_generate as tp
from tests.test_pass_boundary_retry_regressions import client_for


class SpokenOnlyTests(unittest.TestCase):
    batch = [{'type': 'NARRATOR', 'text': 'Alice waited.'},
             {'type': 'SPOKEN', 'text': 'Hello.'},
             {'type': 'NARRATOR', 'text': 'said Alice.'},
             {'type': 'SPOKEN', 'text': 'Goodbye.'}]
    answer = [{'n': 1, 'speaker': 'ALICE'}, {'n': 3, 'speaker': 'ALICE'}]

    def run_batch(self, answers, batch=None, retries=1):
        batch = self.batch if batch is None else batch
        client, calls = client_for(answers)
        result = tp.attribute_batch(client, 'm', batch, LLMGenParams(max_tokens=500),
            ['ALICE'], max_retries=retries, entries_provider=make_provider('michel2_spoken'),
            surround={'entries': [{**e, 'n': i} for i,e in enumerate(batch)]})
        return result, calls

    def test_sparse_labels_restore_narration_and_preserve_text(self):
        out, calls = self.run_batch([(self.answer[::-1], 'stop')])
        self.assertEqual(['NARRATOR', 'ALICE', 'NARRATOR', 'ALICE'], [e['speaker'] for e in out])
        self.assertEqual([e['text'] for e in self.batch], [e['text'] for e in out])
        prompt = calls[0]['messages'][-1]['content']
        self.assertIn('said Alice.', prompt)
        self.assertNotIn('[2]', prompt)
        self.assertIn('|1|', prompt)
        self.assertIn('|3|', prompt)

    def test_all_narration_requires_no_model(self):
        out, calls = self.run_batch([], self.batch[:1])
        self.assertEqual([{'speaker': 'NARRATOR', 'text': 'Alice waited.'}], out)
        self.assertEqual([], calls)

    def test_duplicate_missing_outside_and_narration_indexes_are_rejected(self):
        for rows in (self.answer[:1], [self.answer[0]]*2,
                     [{'n': 0, 'speaker': 'ALICE'}, self.answer[1]],
                     [self.answer[0], {'n': 9, 'speaker': 'ALICE'}],
                     [{'n': True, 'speaker': 'ALICE'}, self.answer[1]]):
            with self.subTest(rows=rows):
                expanded, report = get_expanded_spoken_answer(self.batch, rows)
                self.assertIsNone(expanded)
                self.assertFalse(report['passed'])

    def test_wrong_count_still_subdivides_without_repeating_window(self):
        client, calls = client_for([(self.answer[:1], 'stop')] * 4)
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, 'm', self.batch, LLMGenParams(max_tokens=500),
                ['ALICE'], max_retries=3, entries_provider=make_provider('michel2_spoken'),
                surround={'entries': self.batch})
        self.assertEqual(1, len(calls))

    def test_speaker_trait_fields_survive_binding(self):
        rows = [{**row, 'gender': 'female', 'age_group': 'adult', 'ageless': False}
                for row in self.answer]
        expanded, error = get_expanded_spoken_answer(self.batch, rows)
        self.assertIsNone(error)
        self.assertEqual('female', expanded[1]['gender'])

    def test_exact_schema_allows_only_sparse_spoken_indexes(self):
        _, calls = self.run_batch([(self.answer, 'stop')])
        schema = calls[0]['response_format']['json_schema']['schema']
        self.assertEqual(2, schema['minItems'])
        self.assertEqual(2, schema['maxItems'])
        self.assertEqual([1, 3], schema['items']['properties']['n']['enum'])

    def test_harmless_narration_rows_are_dropped_without_mutating_answer(self):
        rows = [{'n': 0, 'speaker': 'NARRATOR'}, *self.answer,
                {'n': 2, 'speaker': 'NARRATOR'}]
        out, calls = self.run_batch([(rows, 'stop')])
        self.assertEqual(1, len(calls))
        self.assertEqual(['NARRATOR', 'ALICE', 'NARRATOR', 'ALICE'], [e['speaker'] for e in out])
        self.assertEqual(4, len(rows))
        for bad in (rows + [rows[0]], [{**rows[0], 'speaker': 'ALICE'}, *self.answer]):
            self.assertFalse(get_expanded_spoken_answer(self.batch, bad)[1]['passed'])
