"""Future context stays unknown; known context uses the real request renderer."""
import copy
import math
import unittest
from unittest.mock import patch
import generate_script as gs


class RequestContextPreflightTests(unittest.TestCase):
    def test_future_context_cannot_be_reported_as_fitting_from_fixed_allowance(self):
        report = gs.build_book_request_preflight(['A short opening.', 'A short ending.'],
            'system', '{context}\n{chunk}', 500, 8192, 1)
        self.assertIsNone(report['predicted_fits'])
        self.assertTrue(report['static_predicted_fits'])
        self.assertFalse(report['context_complete'])
        self.assertEqual([True, False], [row['context_known'] for row in report['requests']])

    def test_known_large_roster_and_tail_are_fully_included_without_input_mutation(self):
        prior = [{'speaker': f'CHARACTER_{i}', 'text': 'A prior line.', 'instruct': ''}
                 for i in range(500)]
        prior[-1]['text'] = 'A long prior passage. ' * 600
        original = copy.deepcopy(prior)
        template = 'CONTEXT\n{context}\nTARGET\n{chunk}'
        chunks = ['A short opening.', 'A short ending.']
        report = gs.build_book_request_preflight(chunks, 'system', template, 500, 8192, 1,
                                                 previous_entries_by_chunk={2: prior})
        self.assertIs(report['predicted_fits'], False)
        self.assertTrue(report['context_complete'])
        actual = gs.build_chunk_request_prompt(chunks[1], 2, 2, template, prior)
        self.assertIn('CHARACTER_0', actual)
        self.assertIn(prior[-1]['text'], actual)
        self.assertEqual(math.ceil((len('system') + len(actual) + 2000) / 3),
                         report['requests'][1]['prompt_tokens'])
        self.assertEqual(original, prior)

    def test_actual_process_chunk_uses_the_same_complete_prompt(self):
        prior = [{'speaker': 'ALICE', 'text': 'Earlier source. ' * 700, 'instruct': 'Quiet.'}]
        params = gs.LLMGenParams(system_prompt='system', user_prompt_template='{context}\n{chunk}')
        expected = gs.build_chunk_request_prompt('The room was quiet.', 2, 2,
                                                params.user_prompt_template, prior)
        with patch.object(gs, 'call_llm_for_entries', return_value=[]) as call:
            self.assertEqual([], gs.process_chunk(object(), 'model', 'The room was quiet.', 2, 2,
                                                 params, previous_entries=prior, max_retries=0))
        self.assertEqual(expected, call.call_args.args[3])
