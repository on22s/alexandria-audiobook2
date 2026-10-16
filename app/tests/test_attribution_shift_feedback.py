"""Opt-in retry wording for generic adjacent shifts, without label repair."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import attribution_prompt_variants as apv
import generate_script as gs
import pass_quality as pq
import three_pass_generate as tp

FROZEN = [{'type': 'SPOKEN', 'text': 'Hello,', 'uid': 'quote', 'custom': {'keep': [1]}},
          {'type': 'NARRATOR', 'text': 'said the lad.', 'uid': 'tag'}]
SHIFTED = [{'n': 0, 'speaker': 'NARRATOR'}, {'n': 1, 'speaker': 'TOM'}]
GOOD = [{'n': 0, 'speaker': 'TOM'}, {'n': 1, 'speaker': 'NARRATOR'}]
HINT = 'Possible adjacent-row label displacement:'


class FeedbackReportTests(unittest.TestCase):
    def report(self, frozen=FROZEN, answer=SHIFTED):
        return pq.validate_attribution(frozen, answer)

    def test_generic_pair_changes_only_existing_message_and_copies_everything(self):
        report = self.report()
        report['extra'] = {'values': [1]}
        before = copy.deepcopy((FROZEN, SHIFTED, report))
        result = pq.get_attribution_shift_feedback_report(FROZEN, SHIFTED, report)
        self.assertIn('output n=0', result['findings'][0]['message'])
        self.assertIn('output n=1', result['findings'][0]['message'])
        self.assertIn('Do not assume', result['findings'][0]['message'])
        without_message = copy.deepcopy(result)
        without_message['findings'][0]['message'] = report['findings'][0]['message']
        self.assertEqual(report, without_message)
        self.assertEqual(before, (FROZEN, SHIFTED, report))
        result['extra']['values'].append(2)
        self.assertEqual([1], report['extra']['values'])

    def test_wrong_name_empty_quote_and_unknown_tag_do_not_assert_name_truth(self):
        for quote, tag in (('NARRATOR', 'WRONG PERSON'), ('', 'TOM'), ('NARRATOR', 'UNKNOWN')):
            answer = [{'n': 0, 'speaker': quote}, {'n': 1, 'speaker': tag}]
            result = pq.get_attribution_shift_feedback_report(FROZEN, answer, self.report(answer=answer))
            message = result['findings'][0]['message']
            self.assertIn(HINT, message)
            self.assertNotIn(f'assign {tag}', message)
            self.assertFalse(result['passed'])

    def test_native_reordering_and_coercion_use_logical_output_indices(self):
        answer = [{**SHIFTED[1], 'n': 1.0}, {**SHIFTED[0], 'n': '00'}]
        result = pq.get_attribution_shift_feedback_report(FROZEN, answer, self.report(answer=answer))
        self.assertIn('output n=0 labels the spoken line', result['findings'][0]['message'])
        self.assertEqual(['00', 1.0], [row['n'] for row in pq.index_head_check(FROZEN, answer)[2]])

    def test_missing_duplicate_boolean_outside_and_partial_answers_do_not_hint(self):
        for answer in (SHIFTED[:1], [SHIFTED[0]] * 2,
                       [{**SHIFTED[0], 'n': True}, SHIFTED[1]],
                       [{**SHIFTED[0], 'n': 5}, SHIFTED[1]], None):
            report = self.report(answer=answer)
            self.assertEqual(report, pq.get_attribution_shift_feedback_report(FROZEN, answer, report))

    def test_nonadjacent_reversed_types_and_single_failure_do_not_hint(self):
        for frozen, answer in ((FROZEN, GOOD),
                               (FROZEN, [GOOD[0], SHIFTED[1]]),
                               (FROZEN[::-1], SHIFTED),
                               ([FROZEN[0], FROZEN[0], FROZEN[1]],
                                [SHIFTED[0], {'n': 1, 'speaker': 'TOM'}, {'n': 2, 'speaker': 'TOM'}])):
            report = self.report(frozen, answer)
            self.assertEqual(report, pq.get_attribution_shift_feedback_report(frozen, answer, report))

    def test_only_exact_failed_report_is_eligible_and_repeated_enrichment_is_stable(self):
        for passed in (True, None, 0):
            report = {**self.report(), 'passed': passed}
            self.assertEqual(report, pq.get_attribution_shift_feedback_report(FROZEN, SHIFTED, report))
        first = pq.get_attribution_shift_feedback_report(FROZEN, SHIFTED, self.report())
        self.assertEqual(first, pq.get_attribution_shift_feedback_report(FROZEN, SHIFTED, first))

    def test_multiple_pairs_and_unrelated_findings_preserve_codes_and_positions(self):
        frozen = FROZEN * 2
        answer = [dict(row, n=i) for i, row in enumerate(SHIFTED * 2)]
        report = self.report(frozen, answer)
        report['findings'].append({'code': 'unrelated', 'message': 'Other gate still fails.'})
        result = pq.get_attribution_shift_feedback_report(frozen, answer, report)
        self.assertEqual([f['code'] for f in report['findings']], [f['code'] for f in result['findings']])
        self.assertIn('output n=2', result['findings'][2]['message'])
        self.assertEqual(report['findings'][-1], result['findings'][-1])
        self.assertFalse(result['passed'])

    def test_proper_tags_and_trait_metadata_remain_unmodified(self):
        frozen = copy.deepcopy(FROZEN)
        frozen[1]['text'] = 'said Tom.'
        answer = copy.deepcopy(SHIFTED)
        for row in answer:
            row.update(why='synthetic evidence', gender='male', age_group='adult', custom={'v': [1]})
        before = copy.deepcopy((frozen, answer))
        result = pq.get_attribution_shift_feedback_report(frozen, answer, self.report(frozen, answer))
        self.assertIn(HINT, result['findings'][0]['message'])
        self.assertEqual(before, (frozen, answer))


class Client:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return NS(choices=[NS(message=NS(content=json.dumps(next(self.answers))),
                              finish_reason='stop')], usage=None)


class FeedbackNativeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(gs, 'get_response_log_path', side_effect=lambda n: str(self.path / n)))
        stack.enter_context(patch.object(gs.time, 'sleep'))
        stack.enter_context(patch.object(apv, 'rolling_summary', return_value='synthetic summary'))
        stack.enter_context(patch.dict(os.environ, {'JUDGE_WHY_PATH': str(self.path / 'why.jsonl')}))

    def run_batch(self, enabled=None, variant=None, answers=None, frozen=None, **kwargs):
        frozen = copy.deepcopy(FROZEN if frozen is None else frozen)
        client = Client(answers if answers is not None else [SHIFTED, GOOD])
        attempts = []
        options = {} if enabled is None else {'targeted_shift_feedback': enabled}
        if variant:
            options.update(entries_provider=apv.make_provider(variant),
                           surround={'entries': [{**row, 'n': i} for i, row in enumerate(frozen)]})
        result = tp.attribute_batch(client, 'synthetic', frozen,
            gs.LLMGenParams(max_tokens=500, temperature=.2, api_retry_limit=0), ['TOM'],
            max_retries=kwargs.pop('max_retries', 1), attempt_observer=attempts.append,
            **options, **kwargs)
        return result, client.calls, attempts

    def test_default_false_requests_reports_and_results_match_and_enabled_first_is_exact(self):
        ordinary = self.run_batch()
        disabled = self.run_batch(False)
        enabled = self.run_batch(True)
        self.assertEqual(ordinary[:2], disabled[:2])
        stable_attempts = lambda rows: [{k: v for k, v in row.items()
            if k not in ('elapsed_seconds', 'request_id')} for row in rows]
        self.assertEqual(stable_attempts(ordinary[2]), stable_attempts(disabled[2]))
        self.assertEqual(ordinary[0], enabled[0])
        self.assertEqual(ordinary[1][0], enabled[1][0])
        self.assertNotIn(HINT, ordinary[1][1]['messages'][-1]['content'])
        self.assertIn(HINT, enabled[1][1]['messages'][-1]['content'])
        self.assertEqual({k: v for k, v in ordinary[1][1].items() if k != 'messages'},
                         {k: v for k, v in enabled[1][1].items() if k != 'messages'})
        self.assertEqual(['quote', 'tag'], [row['uid'] for row in enabled[0]])

    def test_every_builtin_provider_preserves_first_request_and_schema(self):
        for variant in apv.VARIANTS:
            with self.subTest(variant=variant):
                good = [dict(row, why='The passage identifies the speaker.') for row in GOOD]
                answers = [[SHIFTED[0]], [good[0]]] if variant == 'michel2_spoken' else [SHIFTED, good]
                off = self.run_batch(False, variant, answers)
                on = self.run_batch(True, variant, answers)
                self.assertEqual(off[0], on[0])
                self.assertEqual(off[1][0], on[1][0])
                self.assertEqual(len(off[1]), len(on[1]))
                if variant == 'michel2_spoken':
                    self.assertNotIn(HINT, on[1][1]['messages'][-1]['content'])
                else:
                    self.assertIn(HINT, on[1][1]['messages'][-1]['content'])

    def test_sparse_named_narration_refuses_before_enrichment(self):
        with patch.object(tp, 'get_attribution_shift_feedback_report', wraps=pq.get_attribution_shift_feedback_report) as helper:
            with self.assertRaises(tp.PassExhausted) as raised:
                self.run_batch(True, 'michel2_spoken', [SHIFTED], max_retries=0)
        helper.assert_not_called()
        self.assertIsNone(raised.exception.last_entries)

    def test_no_spoken_rows_preserve_zero_requests(self):
        result, calls, _ = self.run_batch(True, 'michel2_spoken', [], [FROZEN[1]])
        self.assertEqual([], calls)
        self.assertEqual('NARRATOR', result[0]['speaker'])

    def test_judge_only_failure_is_not_relabelled_as_shift_or_accepted(self):
        result, calls, attempts = self.run_batch(True, 'judge', [GOOD, [dict(r, why='Context evidence.') for r in GOOD]])
        self.assertEqual('TOM', result[0]['speaker'])
        self.assertEqual(['missing_judge_reason'], attempts[0]['failure_codes'])
        self.assertNotIn(HINT, calls[1]['messages'][-1]['content'])

    def test_traits_and_frozen_metadata_return_without_transfer(self):
        good = [{**GOOD[0], 'gender': 'male', 'age_group': 'adult', 'ageless': False}, GOOD[1]]
        before = copy.deepcopy((FROZEN, SHIFTED, good))
        result, _, _ = self.run_batch(True, answers=[SHIFTED, good], speaker_traits=True)
        self.assertEqual('male', result[0]['speaker_gender'])
        self.assertNotIn('speaker_gender', result[1])
        self.assertEqual({'keep': [1]}, result[0]['custom'])
        self.assertEqual(before, (FROZEN, SHIFTED, good))

    def test_unrelated_whole_answer_failure_still_refuses_after_pair_correction(self):
        frozen = [*copy.deepcopy(FROZEN), {'type': 'NARRATOR', 'text': 'The room was quiet.'}]
        answers = [[*SHIFTED, {'n': 2, 'speaker': 'TOM'}],
                   [*GOOD, {'n': 2, 'speaker': 'TOM'}]]
        with self.assertRaises(tp.PassExhausted) as raised:
            self.run_batch(True, frozen=frozen, answers=answers)
        self.assertEqual(answers[-1], raised.exception.last_entries)

    def test_existing_proper_name_repair_remains_separate_from_feedback(self):
        frozen = copy.deepcopy(FROZEN)
        frozen[1]['text'] = 'said Tom.'
        result, calls, attempts = self.run_batch(True, frozen=frozen, answers=[SHIFTED],
            source_text='“Hello,” said Tom.', repair_exact_shift=True, max_retries=0)
        self.assertEqual(['TOM', 'NARRATOR'], [row['speaker'] for row in result])
        self.assertEqual(1, len(calls))
        self.assertNotIn(HINT, calls[0]['messages'][-1]['content'])
        self.assertIn('accepted_entry_transform', attempts[0])
        self.assertFalse(attempts[0]['raw_native_report']['passed'])
        self.assertTrue(attempts[0]['full_provider_report']['passed'])

    def test_keep_and_fallback_semantics_do_not_change(self):
        for policy in ('keep', 'fallback'):
            with self.subTest(policy=policy):
                answer = [SHIFTED[0], GOOD[1]] if policy == 'keep' else SHIFTED
                off = self.run_batch(False, answers=[answer], max_retries=0,
                                     on_exhaustion=policy, keep_scope='batch')
                on = self.run_batch(True, answers=[answer], max_retries=0,
                                    on_exhaustion=policy, keep_scope='batch')
                self.assertEqual(off[:2], on[:2])
                self.assertEqual(off[2][0]['failure_codes'], on[2][0]['failure_codes'])
        for enabled in (False, True):
            with self.assertRaises(tp.PassExhausted) as raised:
                self.run_batch(enabled, answers=[SHIFTED], max_retries=0,
                               on_exhaustion='keep', keep_scope='batch')
            self.assertEqual(SHIFTED, raised.exception.last_entries)

    def test_runtime_subdivision_forwards_only_native_keyword(self):
        replies = [SHIFTED[:1], [{'n': 0, 'speaker': 'TOM'}], [{'n': 0, 'speaker': 'NARRATOR'}]]
        native = tp.attribute_batch
        with patch.object(tp, 'get_exhausted_runtime_batch_ranges', return_value=[(0, 1), (1, 2)]), \
                patch.object(tp, 'attribute_batch', wraps=native) as calls:
            result, _, _ = self.run_batch(True, answers=replies, max_retries=0)
        self.assertEqual(['TOM', 'NARRATOR'], [r['speaker'] for r in result])
        self.assertEqual(3, calls.call_count)
        self.assertTrue(all(c.kwargs['targeted_shift_feedback'] for c in calls.call_args_list))

    def test_voting_forwards_opt_in_to_each_sample(self):
        client = Client([SHIFTED, GOOD] * 3)
        native = tp.attribute_batch
        with patch.object(tp, 'attribute_batch', wraps=native) as calls:
            result, confidence = tp.attribute_batch_voted(client, 'synthetic', copy.deepcopy(FROZEN),
                gs.LLMGenParams(max_tokens=500, temperature=.2), ['TOM'], votes=3,
                max_retries=1, targeted_shift_feedback=True)
        self.assertEqual(['TOM', 'NARRATOR'], [r['speaker'] for r in result])
        self.assertEqual([1., 1.], confidence)
        self.assertEqual(3, calls.call_count)
        self.assertTrue(all(c.kwargs['targeted_shift_feedback'] for c in calls.call_args_list))
