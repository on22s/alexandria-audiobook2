"""Synthetic source and provider responses exercise quote state and retry policy."""
import json
import os
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams
from literal_fidelity import audit_literal_words


def client_for(responses):
    calls = []
    responses = iter(responses)
    def create(**kwargs):
        calls.append(kwargs)
        entries, reason = next(responses)
        return NS(choices=[NS(message=NS(content=json.dumps(entries)), finish_reason=reason)],
                  usage=NS(prompt_tokens=20, completion_tokens=20))
    return NS(chat=NS(completions=NS(create=create))), calls


class QuoteBoundaryTests(unittest.TestCase):
    def test_continuation_is_typed_without_a_model_and_preserves_words(self):
        for text in ('Still speaking here.”', 'Still speaking here.', 'Still speaking here.” Then she left.'):
            with self.subTest(text=text):
                analysis = tp.analyze_outer_quote_regions(text, initial_depth=1, allow_open_end=True)
                out = tp.segment_chunk_adaptively(None, 'm', text, LLMGenParams(segmentation='auto'), quote_analysis=analysis)
                self.assertEqual('SPOKEN', out[0]['type'])
                self.assertTrue(tp.validate_segment_for_params(text, out, LLMGenParams(), analysis)['passed'])
                self.assertTrue(audit_literal_words(text, out)['passed'])

    def test_llm_segment_and_context_rescue_validate_carried_quote_state(self):
        text = 'Still speaking here.”'
        analysis = tp.analyze_outer_quote_regions(text, initial_depth=1)
        for context in (False, True):
            with self.subTest(context=context):
                client, calls = client_for([([{'type': 'SPOKEN', 'text': text}], 'stop')])
                params = LLMGenParams(segmentation='llm', max_tokens=500)
                if context:
                    out = tp.segment_chunk_with_context(client, 'm', text, 'Prior context.', 'Later context.', params, quote_analysis=analysis)
                else:
                    out = tp.segment_chunk(client, 'm', text, params, quote_analysis=analysis)
                self.assertEqual([{'type': 'SPOKEN', 'text': 'Still speaking here.'}], out)
                self.assertEqual(1, len(calls))

    def test_exhausted_model_recovers_only_validated_continuation_and_records_resolution(self):
        text = 'Still speaking here.”'
        analysis = tp.analyze_outer_quote_regions(text, initial_depth=1)
        sink = []
        with patch.object(tp, 'segment_chunk', return_value=[]):
            out = tp.segment_chunk_adaptively(None, 'm', text, LLMGenParams(segmentation='llm'), quote_analysis=analysis, resolution_sink=sink)
            self.assertTrue(audit_literal_words(text, out)['passed'])
            self.assertEqual(['quote_continuation_fallback'], sink)
            with patch.object(tp, 'validate_segment_for_params', return_value={'passed': False}), patch.object(tp, 'split_failed_chunk', return_value=None):
                self.assertEqual([], tp.segment_chunk_adaptively(None, 'm', text, LLMGenParams(segmentation='llm'), quote_analysis=analysis))

    def test_adaptive_halves_keep_depth_and_recombined_fidelity(self):
        text = 'Before. “First synthetic sentence. Second synthetic sentence.” After.'
        parts = ('Before. “First synthetic sentence.', 'Second synthetic sentence.” After.')
        seen = []
        def segment(_client, _model, chunk, params, **kwargs):
            if chunk == text:
                return []
            analysis = kwargs['quote_analysis']
            seen.append(analysis)
            return analysis['regions']
        with patch.object(tp, 'segment_chunk', side_effect=segment), patch.object(tp, 'split_failed_chunk', return_value=parts):
            out = tp.segment_chunk_adaptively(None, 'm', text, LLMGenParams(segmentation='llm'))
        self.assertEqual([0, 1], [a['initial_depth'] for a in seen])
        self.assertEqual([1, 0], [a['final_depth'] for a in seen])
        self.assertTrue(tp.validate_segment_for_params(text, out, LLMGenParams())['passed'])
        self.assertTrue(audit_literal_words(text, out)['passed'])

    def test_whole_book_continues_across_long_monologue(self):
        source = '“Alice speaks. ' + 'Synthetic speech continues for another moment. ' * 35 + 'Youth matters.”'
        def attribute(_client, _model, frozen, *args, **kwargs):
            return ([{'speaker': 'ALICE' if e['type'] == 'SPOKEN' else 'NARRATOR', 'text': e['text']} for e in frozen], [1.0] * len(frozen))
        def instruct(_client, _model, named, *args, **kwargs):
            return [{**e, 'instruct': 'Neutral.'} for e in named]
        with tempfile.TemporaryDirectory() as tmp, patch.object(tp, 'attribute_batch_voted', side_effect=attribute), patch.object(tp, 'instruct_batch', side_effect=instruct):
            out = tp.run_three_pass(None, 'm', source, LLMGenParams(segmentation='auto'), 150, output_path=os.path.join(tmp, 'script.json'))
            self.assertTrue(out)
            self.assertTrue(all(e['speaker'] == 'ALICE' for e in out))
            self.assertTrue(audit_literal_words(source, out)['passed'])


class AttributionCountRetryTests(unittest.TestCase):
    frozen = [{'type': 'SPOKEN', 'text': 'Alice says hello.'}, {'type': 'SPOKEN', 'text': 'Alice says goodbye.'}]
    valid = [{'n': 0, 'speaker': 'ALICE'}, {'n': 1, 'speaker': 'ALICE'}]

    def test_default_provider_wrong_count_stops_after_one_call(self):
        from attribution_prompt_variants import make_provider
        client, calls = client_for([(self.valid[:1], 'stop')] * 4)
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, 'm', self.frozen, LLMGenParams(max_tokens=500),
                               ['ALICE'], max_retries=3,
                               entries_provider=make_provider('michel2_full'),
                               surround={'entries': self.frozen, 'before': '', 'after': ''})
        self.assertEqual(1, len(calls))

    def test_default_provider_truncation_still_increases_budget(self):
        from attribution_prompt_variants import make_provider
        client, calls = client_for([(self.valid[:1], 'length'), (self.valid, 'stop')])
        out = tp.attribute_batch(client, 'm', self.frozen,
                                 LLMGenParams(max_tokens=100, hard_max_tokens=1000),
                                 ['ALICE'], max_retries=3,
                                 entries_provider=make_provider('michel2_full'),
                               surround={'entries': self.frozen, 'before': '', 'after': ''})
        self.assertEqual(2, len(calls))
        self.assertGreater(calls[1]['max_tokens'], calls[0]['max_tokens'])
        self.assertEqual([e['text'] for e in self.frozen], [e['text'] for e in out])

    def test_default_provider_name_gate_still_retries(self):
        from attribution_prompt_variants import make_provider
        invalid = [{'n': 0, 'speaker': 'BOB'}, {'n': 1, 'speaker': 'BOB'}]
        client, calls = client_for([(invalid, 'stop')] * 2)
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, 'm', self.frozen, LLMGenParams(max_tokens=500),
                               ['ALICE'], max_retries=1, source_text='Alice says hello.',
                               entries_provider=make_provider('michel2_full'),
                               surround={'entries': self.frozen, 'before': '', 'after': ''})
        self.assertEqual(2, len(calls))

    def test_wrong_count_stops_same_window_after_one_call(self):
        client, calls = client_for([(self.valid[:1], 'stop')] * 5)
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, 'm', self.frozen, LLMGenParams(max_tokens=500), ['ALICE'], max_retries=3)
        self.assertEqual(1, len(calls))

    def test_truncated_count_keeps_budget_retry_and_source_binding(self):
        client, calls = client_for([(self.valid[:1], 'length'), (self.valid, 'stop')])
        out = tp.attribute_batch(client, 'm', self.frozen, LLMGenParams(max_tokens=100, hard_max_tokens=1000), ['ALICE'], max_retries=3)
        self.assertEqual(2, len(calls))
        self.assertGreater(calls[1]['max_tokens'], calls[0]['max_tokens'])
        self.assertEqual([e['text'] for e in self.frozen], [e['text'] for e in out])

    def test_name_and_narrator_gates_still_reject_and_retry(self):
        for frozen, invalid in ((self.frozen, [{'n': 0, 'speaker': 'BOB'}, {'n': 1, 'speaker': 'BOB'}]),
                                ([{'type': 'NARRATOR', 'text': 'Alice walked away.'}], [{'n': 0, 'speaker': 'ALICE'}])):
            with self.subTest(invalid=invalid):
                client, calls = client_for([(invalid, 'stop')] * 2)
                with self.assertRaises(tp.PassExhausted):
                    tp.attribute_batch(client, 'm', frozen, LLMGenParams(max_tokens=500), ['ALICE'], max_retries=1, source_text='Alice says hello. Alice walked away.')
                self.assertEqual(2, len(calls))

    def test_orchestrator_subdivides_count_failure_and_binds_all_source_rows(self):
        calls = []
        def create(**kwargs):
            batch = json.loads(kwargs['messages'][-1]['content'])
            calls.append(len(batch))
            rows = [{'n': e['n'], 'speaker': 'ALICE' if e['type'] == 'SPOKEN' else 'NARRATOR'} for e in batch]
            if len(batch) > 1:
                rows = rows[:-1]
            return NS(choices=[NS(message=NS(content=json.dumps(rows)), finish_reason='stop')], usage=None)
        client = NS(chat=NS(completions=NS(create=create)))
        def instruct(_client, _model, named, *args, **kwargs):
            return [{**e, 'instruct': 'Neutral.'} for e in named]
        source = 'Alice said. “Hello.” “Goodbye.”'
        params = LLMGenParams(segmentation='auto', max_tokens=500, user_prompt_template='{batch}')
        with tempfile.TemporaryDirectory() as tmp, patch.object(tp, 'instruct_batch', side_effect=instruct):
            out = tp.run_three_pass(client, 'm', source, params, 3000, output_path=os.path.join(tmp, 'script.json'), on_exhaustion='keep')
            self.assertTrue(audit_literal_words(source, out)['passed'])
            self.assertEqual(['NARRATOR', 'ALICE', 'ALICE'], [e['speaker'] for e in out])
        self.assertEqual(1, calls.count(3))
        self.assertTrue(any(size == 1 for size in calls))
