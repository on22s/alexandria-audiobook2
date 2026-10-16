"""Exhaustion-only labels remain source bound and visibly incomplete."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import generate_script as generation
from generate_script import LLMGenParams
import review_script as review
from generation_checkpoint_deltas import load_generation_checkpoint_document
from completed_review_receipt import get_review_receipt_path


SOURCE = [
    {'speaker': 'BOB', 'text': 'Please leave the blue cup.', 'instruct': '', 'type': 'SPOKEN',
     'uid': 'first', 'pause_after': 900, 'provenance': {'span': [3, 8]}},
    {'speaker': 'NARRATOR', 'text': 'The door swung gently.', 'instruct': '', 'type': 'NARRATOR', 'uid': 'second'},
    {'speaker': 'CAROL', 'text': 'I will return before dawn.', 'instruct': '', 'type': 'SPOKEN', 'uid': 'third'}]


def get_reply(source=SOURCE):
    rows = [{key: row[key] for key in ('speaker', 'text', 'instruct')} for row in source]
    rows[0].update(speaker='ALICE', instruct='Calmly.')
    rows[1]['text'] = 'The door swung wildly.'
    return rows


def get_response(text, reason='stop'):
    return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
        'created': 1, 'model': 'fixture', 'choices': [{'index': 0,
        'message': {'role': 'assistant', 'content': text}, 'finish_reason': reason}],
        'usage': {'prompt_tokens': 30, 'completion_tokens': 30, 'total_tokens': 60}})


class PartialRetentionTests(unittest.TestCase):
    def run_review(self, replies, source=SOURCE, reasons=None, params=None,
                   enabled=True, observer=True, switched=False, logging_error=False):
        calls, records = [], []
        answers = iter(zip(replies, reasons or ['stop'] * len(replies)))
        before = copy.deepcopy(source)
        def respond(request):
            calls.append(json.loads(request.content))
            answer, reason = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return get_response(answer if isinstance(answer, str) else json.dumps(answer), reason)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client = stack.enter_context(OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture',
                max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
            client.switched = switched
            if logging_error:
                stack.enter_context(patch.object(generation, 'get_response_log_path', side_effect=OSError('fixture log refusal')))
            else:
                stack.enter_context(patch.object(generation, 'get_response_log_path', return_value=str(Path(directory, 'response.log'))))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(generation.time, 'sleep'))
            output = review.review_batch(client, 'fixture', source, 1, 1,
                params or LLMGenParams(temperature=.7, api_retry_limit=0), max_retries=len(replies) - 1,
                attempt_observer=records.append if observer else None, retain_partial_labels=enabled)
        self.assertEqual(before, source)
        return output, calls, records

    def test_edited_other_row_retains_only_exact_labels_after_all_original_attempts(self):
        reply = get_reply()
        output, calls, records = self.run_review([reply] * 3)
        self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *SOURCE[1:]], output)
        self.assertIsNot(output[0]['provenance'], SOURCE[0]['provenance'])
        self.assertEqual(3, len(calls))
        status = review.get_review_batch_status(output, records)
        self.assertEqual('partial', status['status'])
        self.assertEqual(3, len(status['attempts']))
        self.assertEqual([0], status['partial_retention']['retained_positions'])
        self.assertEqual(3, status['partial_retention']['source_attempt'])
        self.assertEqual(['quality_rejected'] * 3, [r['outcome'] for r in status['attempts']])
        self.assertNotIn('attempt', records[-1])
        disabled, old_calls, _ = self.run_review([reply] * 3, enabled=False)
        self.assertIsNone(disabled)
        self.assertEqual(old_calls, calls)

    def test_full_review_retries_narration_rename_before_complete_sdk_publication(self):
        source = [{'speaker': 'NARRATOR', 'text': 'The rain stopped.', 'instruct': '',
                   'spoken': False, 'uid': 'n1', 'source_span': [4, 21], 'pause_after': 700}]
        bad = [{**source[0], 'speaker': 'ALICE', 'spoken': True}]
        good = [{key: source[0][key] for key in ('speaker', 'text', 'instruct')}]
        output, calls, records = self.run_review([bad, good], source=source)
        self.assertEqual(2, len(calls))
        self.assertEqual(['quality_rejected', 'accepted'], [record['outcome'] for record in records])
        self.assertIn('narrator_renamed', records[0]['failure_codes'])
        self.assertEqual(source, output)
        self.assertIsNot(source[0]['source_span'], output[0]['source_span'])
        self.assertEqual('complete', review.get_review_batch_status(output, records)['status'])

    def test_moved_other_rows_are_skipped_but_unaffected_exact_row_is_retained(self):
        reply = get_reply(); reply[1:] = [reply[2], {key: SOURCE[1][key] for key in ('speaker', 'text', 'instruct')}]
        output, calls, records = self.run_review([reply] * 3)
        self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *SOURCE[1:]], output)
        self.assertEqual([1, 2], [r['n'] for r in records[-1]['skipped_positions']])
        self.assertEqual(3, len(calls))

    def test_extra_unrelated_duplicate_keeps_only_independently_bound_source_rows(self):
        reply = get_reply() + [copy.deepcopy(get_reply()[2])]
        output, calls, records = self.run_review([reply] * 3)
        self.assertEqual(3, len(calls))
        self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *SOURCE[1:]], output)
        status = review.get_review_batch_status(output, records)
        self.assertEqual('partial', status['status'])
        self.assertEqual([0], status['partial_retention']['retained_positions'])
        self.assertEqual(3, len(status['attempts']))
        self.assertEqual(4, len(records))

    def test_missing_tail_keeps_every_source_row_and_marks_partial_after_exhaustion(self):
        output, calls, records = self.run_review([get_reply()[:2]] * 3)
        self.assertEqual(3, len(calls))
        self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *SOURCE[1:]], output)
        self.assertEqual('partial', review.get_review_batch_status(output, records)['status'])
        output[0]['provenance']['span'].append(99)
        self.assertEqual([3, 8], SOURCE[0]['provenance']['span'])

    def test_inserted_or_removed_middle_rows_never_remap_shifted_labels(self):
        reply = get_reply()
        extra = {'speaker': 'INVENTED', 'text': 'An unrelated extra sentence.', 'instruct': ''}
        for rows in ([reply[0], extra, *reply[1:]], [reply[0], reply[2]]):
            with self.subTest(rows=rows):
                output, calls, records = self.run_review([rows] * 3)
                self.assertEqual(3, len(calls))
                self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *SOURCE[1:]], output)
                self.assertEqual([0], review.get_review_batch_status(output, records)['partial_retention']['retained_positions'])
        output, _, _ = self.run_review([reply[1:]] * 3)
        self.assertIsNone(output)

    def test_repeated_other_source_rows_do_not_disqualify_a_unique_changed_row(self):
        source = copy.deepcopy(SOURCE)
        source[2]['text'] = source[1]['text']
        output, calls, records = self.run_review([get_reply(source)] * 3, source=source)
        self.assertEqual(3, len(calls))
        self.assertEqual([{**source[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'}, *source[1:]], output)
        self.assertEqual([0], review.get_review_batch_status(output, records)['partial_retention']['retained_positions'])

    def test_duplicate_identity_anywhere_in_candidate_cannot_authorize_that_label(self):
        reply = get_reply()
        reply[2]['speaker'] = 'DAVE'
        for duplicate in (copy.deepcopy(reply[0]), {**reply[0], 'text': SOURCE[0]['text'][:-1] + '!'}):
            with self.subTest(duplicate=duplicate):
                output, _, records = self.run_review([reply + [duplicate]] * 3)
                self.assertEqual([SOURCE[0], SOURCE[1], {**SOURCE[2], 'speaker': 'DAVE'}], output)
                self.assertEqual([2], review.get_review_batch_status(output, records)['partial_retention']['retained_positions'])

    def test_twenty_six_reply_rows_keep_only_unique_exact_positions_in_twenty_five_source_rows(self):
        source = [{'speaker': 'BOB', 'text': f'Unique spoken sentence number {i}.',
                   'instruct': '', 'type': 'SPOKEN', 'spoken': True, 'uid': str(i),
                   'pause_after': 100 + i, 'provenance': {'row': i}} for i in range(25)]
        reply = [{key: row[key] for key in ('speaker', 'text', 'instruct')} for row in source]
        for row in reply:
            row.update(speaker='ALICE', instruct='Calmly.')
        reply[24]['text'] = 'A lexically different final sentence.'
        reply.append(copy.deepcopy(reply[17]))
        output, calls, records = self.run_review([reply] * 3, source=source)
        safe = [i for i in range(24) if i != 17]
        self.assertEqual(3, len(calls))
        self.assertEqual([{**row, 'speaker': 'ALICE', 'instruct': 'Calmly.'} if i in safe else row
                          for i, row in enumerate(source)], output)
        status = review.get_review_batch_status(output, records)
        self.assertEqual('partial', status['status'])
        self.assertEqual(safe, status['partial_retention']['retained_positions'])

    def test_source_speech_corrects_wrong_narrator_only_after_quality_exhaustion(self):
        source = copy.deepcopy(SOURCE)
        source[0].update(spoken=True, speaker='NARRATOR', type='NARRATOR')
        reply = get_reply(source)
        output, calls, records = self.run_review([reply] * 3, source=source)
        self.assertEqual([{**source[0], 'speaker': 'ALICE', 'instruct': 'Calmly.'},
                          *source[1:]], output)
        self.assertIsNot(source[0]['provenance'], output[0]['provenance'])
        self.assertEqual(3, len(calls))
        status = review.get_review_batch_status(output, records)
        self.assertEqual('partial', status['status'])
        self.assertEqual([0], status['partial_retention']['retained_positions'])
        self.assertEqual(3, status['partial_retention']['source_attempt'])
        self.assertEqual(['quality_rejected'] * 3,
                         [attempt['outcome'] for attempt in status['attempts']])

    def test_partial_labels_cannot_rename_source_narration_despite_model_speech(self):
        source = copy.deepcopy(SOURCE)
        source[0].update(spoken=False, type='SPOKEN')
        output, calls, records = self.run_review([get_reply(source)] * 3, source=source)
        self.assertIsNone(output)
        self.assertEqual(3, len(calls))
        self.assertFalse(any(record.get('event') == review.REVIEW_PARTIAL_EVENT for record in records))

    def test_last_candidate_only_and_later_complete_answer_always_wins(self):
        first = get_reply(); first[0]['speaker'] = 'EARLIER'
        last = get_reply(); last[0]['speaker'] = 'LATEST'
        output, calls, _ = self.run_review([first, first, last])
        self.assertEqual('LATEST', output[0]['speaker']); self.assertEqual(3, len(calls))
        complete = copy.deepcopy(SOURCE); complete[0]['speaker'] = 'COMPLETE'
        output, calls, records = self.run_review([first, complete, last])
        self.assertEqual('COMPLETE', output[0]['speaker']); self.assertEqual(2, len(calls))
        self.assertEqual('complete', review.get_review_batch_status(output, records)['status'])
        self.assertEqual(2, len(records))

    def test_disjoint_attempt_candidates_never_union_earlier_labels(self):
        first = get_reply()
        final = copy.deepcopy(SOURCE)
        final[0]['text'] = 'Please leave the red cup.'
        final[2]['speaker'] = 'FINAL_ONLY'
        output, calls, records = self.run_review([first, first, final])
        self.assertEqual(SOURCE[:2], output[:2])
        self.assertEqual({**SOURCE[2], 'speaker': 'FINAL_ONLY'}, output[2])
        self.assertEqual([2], records[-1]['retained_positions'])
        self.assertEqual(3, len(calls))

    def test_no_optin_or_observer_keeps_direct_list_none_contract(self):
        for kwargs in ({'enabled': False}, {'observer': False}):
            with self.subTest(kwargs=kwargs):
                output, calls, _ = self.run_review([get_reply()] * 3, **kwargs)
                self.assertIsNone(output); self.assertEqual(3, len(calls))

    def test_unique_complete_field_and_narrator_guards_abstain(self):
        bads = []
        rows = get_reply(); rows[2] = copy.deepcopy(rows[0]); bads.append(rows)
        bads.append(get_reply() + [get_reply()[0]])
        for field, value in [('speaker', ''), ('speaker', None), ('instruct', 1), ('text', '')]:
            rows = get_reply(); rows[2][field] = value; bads.append(rows)
        rows = get_reply(); rows[1]['text'] = SOURCE[1]['text']; rows[1]['speaker'] = 'INVENTED'
        rows[2]['text'] = 'A changed final line.'; bads.append(rows)
        for rows in bads:
            with self.subTest(rows=rows):
                output, _, records = self.run_review([rows] * 3)
                self.assertIsNone(output)
                self.assertFalse(any(r.get('event') == review.REVIEW_PARTIAL_EVENT for r in records))
        for text in ('Please leave the blue cup!', '...', '\u0301Words.'):
            source = copy.deepcopy(SOURCE); source[2]['text'] = text
            if text.endswith('!'):source[2]['text'] = source[0]['text'][:-1] + '!'
            output, _, _ = self.run_review([get_reply(source)] * 3, source=source)
            self.assertIsNone(output)

    def test_edited_label_row_symbols_case_contractions_punctuation_and_zero_change_abstain(self):
        for original, changed in [('It costs $5.', 'It costs 5.'), ('Compute 5+6.', 'Compute 5/6.'),
                                  ("Don't stop.", 'Dont stop.'), ('Alice speaks.', 'alice speaks.'),
                                  ('Go 🐈.', 'Go 🐕.'), ('Keep punctuation!', 'Keep punctuation.')]:
            source = copy.deepcopy(SOURCE); source[0]['text'] = original
            reply = get_reply(source); reply[0]['text'] = changed
            with self.subTest(original=original):
                output, _, _ = self.run_review([reply] * 3, source=source)
                self.assertIsNone(output)
        reply = get_reply(); reply[0].update(speaker=SOURCE[0]['speaker'], instruct='')
        output, _, _ = self.run_review([reply] * 3); self.assertIsNone(output)

    def test_malformed_repair_cleanup_and_filter_provenance_never_qualify_retention(self):
        raw = json.dumps(get_reply())
        cases = [raw[:-1] + ',]', raw.replace('}, {', '} {'), raw[:-1],
                 '```json\n' + raw + '\n```', 'Preface: ' + raw,
                 '<think>reasoning</think>' + raw,
                 json.dumps(get_reply()[:1]) + json.dumps(get_reply()[1:]),
                 json.dumps(get_reply() + ['filtered']),
                 json.dumps(get_reply()[:1]) + ' ambiguous prose ' + json.dumps(get_reply()[1:])]
        for payload in cases:
            with self.subTest(payload=payload):
                output, _, records = self.run_review([payload] * 3)
                self.assertIsNone(output)
                self.assertFalse(any(r.get('event') == review.REVIEW_PARTIAL_EVENT for r in records))
        # Valid full replies still use all the native repair paths unchanged.
        complete = copy.deepcopy(SOURCE); complete[0]['speaker'] = 'ALICE'
        payload = json.dumps(complete)[:-1] + ',]'
        output, calls, records = self.run_review([payload])
        self.assertEqual(complete, output); self.assertEqual(1, len(calls))
        self.assertEqual('trailing_commas', records[0]['parse_provenance']['method'])

    def test_literal_control_newline_cleanup_abstains_despite_strict_parse_after_extraction(self):
        source = copy.deepcopy(SOURCE)
        source[0]['text'] = 'Please leave\nthe blue cup.'
        raw = json.dumps(get_reply(source)).replace('\\n', '\n')
        output, calls, records = self.run_review([raw] * 3, source=source)
        self.assertIsNone(output)
        self.assertEqual(3, len(calls))
        for record in records:
            self.assertEqual('strict_json', record['parse_provenance']['method'])
            self.assertFalse(record['parse_provenance']['extraction_unchanged'])

    def test_length_api_logging_switched_and_earlybreak_do_not_recover_stale_candidate(self):
        reply = get_reply()
        request = httpx.Request('POST', 'http://fixture.invalid/v1/chat/completions')
        errors = [httpx.ReadTimeout('fixture timeout', request=request)]
        output, _, _ = self.run_review([reply, reply, errors[0]])
        self.assertIsNone(output)
        output, _, _ = self.run_review([reply] * 3, reasons=['stop', 'stop', 'length'])
        self.assertIsNone(output)
        output, calls, _ = self.run_review([reply] * 3, params=LLMGenParams(temperature=0))
        self.assertIsNone(output); self.assertEqual(2, len(calls))
        for kwargs in ({'switched': True}, {'logging_error': True}):
            output, _, _ = self.run_review([reply] * 3, **kwargs); self.assertIsNone(output)
        output, _, _ = self.run_review([reply, reply, 'not an array'])
        self.assertIsNone(output)

    def test_unknown_codec_and_request_admission_refuse(self):
        from response_codecs import ResponseCodec, get_codec
        native = get_codec('json')
        unknown = ResponseCodec('json', native.extract, native.parse, native.salvage, native.raw_fallback)
        with patch('response_codecs.get_codec', return_value=unknown):
            output, _, _ = self.run_review([get_reply()] * 3)
        self.assertIsNone(output)
        from llm_provider import RunRequestAdmissionError
        with patch.object(generation, 'create_completion', side_effect=RunRequestAdmissionError('fixture refusal')):
            output, calls, _ = self.run_review([get_reply()] * 3)
        self.assertIsNone(output); self.assertEqual([], calls)

    def test_real_failover_adaptive_parts_do_not_opt_into_partial_retention(self):
        from llm_provider import FailoverClient
        source = copy.deepcopy(SOURCE) + [{'speaker': 'DAN', 'text': 'The garden gate stood open.', 'instruct': ''}]
        replies = []
        for part in (source[:2], source[2:]):
            rows = copy.deepcopy(part)
            rows[0]['speaker'] = 'PARTIAL_CANDIDATE'
            rows[1]['text'] += ' Unexpected addition.'
            # The control would qualify if a recursive caller enabled retention.
            self.assertIsNotNone(review.get_review_partial_label_retention(part, rows, None)[0])
            replies += [(rows, 'stop')] * 3
        answers = iter([(source, 'length')] * 3 + replies)
        calls, records = [], []
        def respond(request):
            calls.append(json.loads(request.content))
            rows, reason = next(answers)
            return get_response(json.dumps(rows), reason)
        with tempfile.TemporaryDirectory() as directory, \
             OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk, \
             patch.object(generation, 'get_response_log_path', return_value=str(Path(directory, 'responses.log'))), \
             patch.object(generation.time, 'sleep'), redirect_stdout(io.StringIO()):
            client = FailoverClient(sdk, 'primary', sdk, 'secondary')
            self.assertTrue(client.failover({'category': 'rate_limited'}))
            output = review.review_batch(client, 'primary', source, 1, 1,
                LLMGenParams(temperature=.7, api_retry_limit=0), max_retries=2,
                attempt_observer=records.append, retain_partial_labels=True)
        self.assertIsNone(output)
        self.assertEqual(9, len(calls))
        self.assertTrue(all(body['model'] == 'secondary' for body in calls))
        self.assertFalse(any(record.get('event') == review.REVIEW_PARTIAL_EVENT for record in records))

    def test_literal_policy_and_model_metadata_never_escape_source(self):
        from literal_policy import bind_literal_policy, get_literal_policy
        source = bind_literal_policy(' '.join(r['text'] for r in SOURCE), copy.deepcopy(SOURCE))
        reply = get_reply(); reply[0].update(uid='forged', pause_after=0, provenance={'wrong': True})
        output, _, _ = self.run_review([reply] * 3, source=source)
        self.assertEqual(get_literal_policy(source), get_literal_policy(output))
        self.assertEqual(source[0]['uid'], output[0]['uid'])
        self.assertEqual(source[0]['pause_after'], output[0]['pause_after'])
        self.assertEqual(source[0]['provenance'], output[0]['provenance'])

    def test_parser_detached_metadata_preserves_every_existing_repair_filter_output(self):
        cases = [('[{"x":1}]', [{'x': 1}], 'strict_json', 0),
                 ('[{"x":1} {"y":2}]', [{'x': 1}, {'y': 2}], 'missing_commas', 0),
                 ('[{"x":1},]', [{'x': 1}], 'trailing_commas', 0),
                 ('prefix {"x":1} tail', [{'x': 1}], 'object_extraction', 0),
                 ('[{"x":1},"extra"]', [{'x': 1}], 'strict_json', 1),
                 ('[]', None, 'strict_json', 0), ('invalid', None, 'unparsed', 0),
                 ('', None, 'unparsed', 0)]
        for payload, expected, method, filtered in cases:
            with self.subTest(payload=payload):
                parsed, provenance = generation.get_json_array_parse_result(payload)
                self.assertEqual(expected, parsed)
                self.assertEqual(expected, generation.repair_json_array(payload))
                self.assertEqual({'method': method, 'filtered_entries': filtered}, provenance)
                provenance['method'] = 'changed'
                self.assertEqual(method, generation.get_json_array_parse_result(payload)[1]['method'])

    def test_native_parser_uses_entries_only_interface_without_an_observer(self):
        complete = copy.deepcopy(SOURCE)
        for observed in (False, True):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as directory, \
                 OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(
                            lambda request: get_response(json.dumps(complete))))) as client, \
                 patch.object(generation, 'get_response_log_path', return_value=str(Path(directory, 'responses.log'))), \
                 patch.object(generation, 'repair_json_array', wraps=generation.repair_json_array) as entries_parser, \
                 patch.object(generation, 'get_json_array_parse_result', wraps=generation.get_json_array_parse_result) as provenance_parser, \
                 redirect_stdout(io.StringIO()):
                records = []
                output = generation.call_llm_for_entries(client, 'fixture', 'system', 'user',
                    LLMGenParams(temperature=.7, api_retry_limit=0), 'responses.log', 'fixture', max_retries=0,
                    attempt_observer=records.append if observed else None)
                self.assertEqual(complete, output)
                self.assertEqual(0 if observed else 1, entries_parser.call_count)
                self.assertEqual(1, provenance_parser.call_count)
                if observed:self.assertEqual('strict_json', records[0]['parse_provenance']['method'])
                else:self.assertEqual([], records)


class PartialPublicationTests(unittest.TestCase):
    def assert_parent_report(self, log, directory, *, partial=False, failed=False):
        import core
        from review_report import get_review_report_info
        state = {'running': False, 'logs': [], 'status': '', 'return_code': None}
        def stream(command, cwd, actual_state, **kwargs):
            actual_state['logs'].extend(log.splitlines())
            return 0, ''
        report_dir = Path(directory, 'reports')
        with patch.dict(core.process_state, {'review': state}), \
             patch.object(core, 'REPORTS_DIR', str(report_dir)), \
             patch.object(core, '_init_task_log', return_value=None), \
             patch.object(core, '_stream_subprocess_to_logs', side_effect=stream), \
             patch.object(core, '_llm_summarize_report', side_effect=AssertionError('No model summary')):
            self.assertEqual(0, core.run_process(['synthetic-review'], 'review'))
        self.assertEqual('incomplete' if failed else 'done', state['status'])
        self.assertFalse(state['running'])
        reports = list(report_dir.glob('*.md'))
        self.assertEqual(1, len(reports))
        info = get_review_report_info(reports[0])
        self.assertEqual(failed, info['incomplete'])
        notes = '\n'.join(state['logs'][-2:])
        if partial:
            self.assertIn('Source-bound partial label edits were retained', notes)
            self.assertNotIn('The original entries were preserved', notes)
            self.assertIn('completed-section counts below exclude those partial edits', info['content'])
            self.assertIn('only source-bound label edits were retained', info['content'])
        elif failed:
            self.assertIn('section(s) could not be reviewed. The original entries were preserved;', notes)
            self.assertNotIn('partial label edits were retained', notes)
        else:
            self.assertIn('Task review completed successfully.', notes)
        return info

    def run_main(self, directory, path, client, *, contextual=False, output=None, concurrency=1, partial=True):
        config = {'llm_mode': 'remote', 'llm_remote': {'model_name': 'fixture'},
                  'generation': {'review_batch_size': 3, 'merge_narrators': True}}
        args = ['review', '--input', str(path), '--dedupe-speakers']
        if contextual:args += ['--context-window', '1']
        if output is not None:args += ['--output', str(output)]
        log = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(log))
            stack.enter_context(patch.object(review.sys, 'argv', args))
            for name, value in [('get_runtime_data_dir', directory), ('load_app_config', config),
                                ('ensure_ideal_settings', (True, {}, 'fixture')),
                                ('get_active_llm_config', config['llm_remote']), ('make_run_client', client),
                                ('get_cached_or_benchmarked_concurrency', concurrency),
                                ('get_current_status', {'loaded': False})]:
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(generation, 'get_response_log_path', return_value=str(Path(directory, 'responses.log'))))
            stack.enter_context(patch.object(review, 'wait_for_vram_headroom', side_effect=AssertionError('No GPU query')))
            stack.enter_context(patch.object(review, 'merge_consecutive_narrators',
                side_effect=AssertionError('No incomplete merge') if partial else lambda rows, **kw: (rows, 0)))
            stack.enter_context(patch.object(review, 'dedupe_speakers',
                side_effect=AssertionError('No incomplete dedupe') if partial else lambda *a, **kw: ({}, 0, [])))
            review.main()
        return log.getvalue()

    def test_contextual_and_concurrent_partial_artifacts_keep_failure_no_seal_and_batch_local_status(self):
        for contextual in (True, False):
            with self.subTest(contextual=contextual), tempfile.TemporaryDirectory() as directory:
                other = copy.deepcopy(SOURCE)
                for i, row in enumerate(other):row.update(text='Different source sentence number %d.' % i, uid='other%d' % i)
                original = copy.deepcopy(SOURCE) if contextual else copy.deepcopy(SOURCE) + other
                source = Path(directory, 'script.json'); source.write_text(json.dumps(original))
                calls = []; lock = threading.Lock()
                def respond(request):
                    body = json.loads(request.content)
                    with lock:calls.append(body)
                    if 'Different source sentence number' in body['messages'][-1]['content']:
                        rows = copy.deepcopy(other); rows[0]['speaker'] = 'SECOND_COMPLETE'
                    else:rows = get_reply()
                    return get_response(json.dumps(rows))
                with OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
                    log = self.run_main(directory, source, client, contextual=contextual, concurrency=2)
                saved = json.loads(source.read_text())
                self.assertEqual('ALICE', saved[0]['speaker']); self.assertEqual(SOURCE[1:], saved[1:3])
                checkpoint = load_generation_checkpoint_document(str(source) + '.review_checkpoint.json')
                self.assertEqual([1], checkpoint['failed_batches'])
                self.assertEqual(1, checkpoint['total_stats']['batches_failed'])
                self.assertEqual(0 if contextual else 1, checkpoint['total_stats']['speaker_changed'])
                self.assertFalse(Path(get_review_receipt_path(str(source))).exists())
                self.assertIn('review_partial_label_retention', log)
                self.assertIn('complete review remains pending', log)
                self.assertIn('Partial label retention: 1 batch(es)', log)
                self.assertIn('Completed-batch changes below exclude those partial label edits.', log)
                self.assertIn('Review incomplete:', log)
                self.assertNotIn('No issues found -- script looks clean.', log)
                self.assertNotIn('Task review completed successfully.', log)
                info = self.assert_parent_report(log, directory, partial=True, failed=True)
                if not contextual:
                    self.assertEqual('SECOND_COMPLETE', saved[3]['speaker'])
                    self.assertEqual(4, len(calls))
                else:
                    self.assertEqual(3, len(calls))
                    self.assertIn('No completed-section changes were reported', info['content'])
                    self.assertNotIn('No changes were reported during this pass.', info['content'])

    def test_parent_preserves_original_fallback_and_complete_report_behavior(self):
        for failed in (True, False):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, 'script.json'); path.write_text(json.dumps(SOURCE))
                reply = get_reply() if failed else copy.deepcopy(SOURCE)
                if failed:reply[0].update(speaker=SOURCE[0]['speaker'], instruct='')
                with OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(
                                lambda request: get_response(json.dumps(reply))))) as client:
                    log = self.run_main(directory, path, client, partial=False)
                info = self.assert_parent_report(log, directory, failed=failed)
                self.assertIn('No changes were reported during this pass.', info['content'])

    def test_actual_saved_partial_resume_keeps_existing_input_policy_without_stale_change_stats(self):
        for separate in (False, True):
            with self.subTest(separate=separate), tempfile.TemporaryDirectory() as directory:
                source = Path(directory, 'script.json'); source.write_text(json.dumps(SOURCE))
                output = Path(directory, 'output.json') if separate else source
                calls = []; phase = [0]
                def respond(request):
                    body = json.loads(request.content); calls.append(body)
                    if not phase[0]:return get_response(json.dumps(get_reply()))
                    # Complete answer now succeeds; source labels reflect the existing resume input policy.
                    rows = copy.deepcopy(SOURCE)
                    rows[0].update(speaker='ALICE', instruct='Calmly.')
                    return get_response(json.dumps(rows))
                with OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
                    self.run_main(directory, source, client, output=output if separate else None)
                    first = load_generation_checkpoint_document(str(output) + '.review_checkpoint.json')
                    self.assertEqual(0, first['total_stats']['speaker_changed'])
                    self.assertEqual('ALICE', json.loads(output.read_text())[0]['speaker'])
                    phase[0] = 1
                    resumed_log = self.run_main(directory, source, client,
                        output=output if separate else None, partial=False)
                    self.assertIn('rewinding checkpoint', resumed_log)
                    self.assertEqual(1 if separate else 0, self.get_final_stats(resumed_log)['speaker_changed'])
                self.assertEqual(4, len(calls))
                target = calls[-1]['messages'][-1]['content'].split('SCRIPT ENTRIES TO REVIEW:')[-1]
                self.assertIn('"speaker": "BOB"' if separate else '"speaker": "ALICE"', target)
                self.assertTrue(Path(get_review_receipt_path(str(output))).exists())
                self.assertFalse(Path(str(output) + '.review_checkpoint.json').exists())

    def get_final_stats(self, text):
        # Read the real summary rather than inventing a second accounting implementation.
        import re
        return {'speaker_changed': int(re.search(r'Speaker changed:\s+(\d+)', text).group(1))}

    def test_benchmark_local_and_remote_report_partial_quality_without_extra_model_attempt(self):
        from tests.test_benchmark_review_mode import SOURCE as benchmark_source, run_native_benchmark
        reply = copy.deepcopy(benchmark_source); reply[0]['speaker'] = 'ALICE'; reply[1]['text'] = 'said loudly.'
        for target in ('local', 'thunder'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                with patch.object(generation, 'get_response_log_path', return_value=str(Path(directory, 'responses.log'))):
                    result = run_native_benchmark(target=target, mode=False, reply=reply)
                case = result['report']['cases'][0]
                self.assertEqual('partial', case['review_status']); self.assertEqual('failed', case['status'])
                self.assertFalse(case['quality']['passed']); self.assertTrue(case['quality']['text_loss_passed'])
                self.assertEqual(1, len(case['attempts'])); self.assertEqual(1, len(result['requests']))
                self.assertEqual(review.REVIEW_PARTIAL_EVENT, case['partial_retention']['event'])
                self.assertEqual(result['report'], result['saved_report'])

    def test_shared_algorithm_identity_invalidates_old_receipt_checkpoint_and_benchmark_resume(self):
        from types import SimpleNamespace
        import completed_review_receipt as receipt
        import benchmark_core as benchmark
        from benchmark_runner import get_benchmark_completed_cases
        from tests.test_benchmark_review_mode import ENVIRONMENT, get_config
        args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                               alias_registry=None, remap_voice_config=None)
        config = get_config(False)
        params = LLMGenParams(temperature=.7)
        def fingerprint():
            return review.get_completed_review_fingerprint(config, config['llm_remote'], params,
                                                         args, object(), 'fixture')
        with patch.object(review, 'REVIEW_ALGORITHM_VERSION', 5):old = fingerprint()
        current = fingerprint()
        self.assertNotEqual(old, current)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'source.json'); source.write_text(json.dumps(SOURCE))
            sha = receipt.get_review_entries_fingerprint(SOURCE)
            receipt.save_completed_review_receipt(str(source), str(source), sha, sha, old)
            self.assertTrue(receipt.get_completed_review_match(str(source), str(source), sha, old)[0])
            self.assertFalse(receipt.get_completed_review_match(str(source), str(source), sha, current)[0])
            review.save_checkpoint(str(source), 1, 1, 3, 0, copy.deepcopy(SOURCE), {}, SOURCE[-2:],
                                   [3], [], sha, execution_sha256=old)
            self.assertIsNotNone(review.load_checkpoint(str(source), 1, 3, 0, SOURCE, old))
            self.assertIsNone(review.load_checkpoint(str(source), 1, 3, 0, SOURCE, current))
            manifest = {'schema_version': 1, 'stage': 'script_review', 'targets': ['local'],
                        'fixtures': [{'id': 'fixture', 'sha256': 'fixture'}]}
            base = benchmark.build_environment_fingerprint('local', ENVIRONMENT)
            with patch.object(receipt, 'REVIEW_ALGORITHM_VERSION', 5):
                prior_env = benchmark.get_benchmark_execution_environment(manifest, base, config)
            new_env = benchmark.get_benchmark_execution_environment(manifest, base, config)
            self.assertNotEqual(prior_env, new_env)
            labels_config = copy.deepcopy(config); labels_config['prompts']['review_labels_only'] = True
            self.assertNotEqual(new_env, benchmark.get_benchmark_execution_environment(manifest, base, labels_config))
            report = benchmark.build_benchmark_report(manifest, prior_env)
            report['cases'] = [{'fixture_id': 'fixture', 'repetition': 1, 'status': 'failed'}]
            self.assertEqual({('fixture', 1)}, get_benchmark_completed_cases(report))
            path = str(Path(directory, 'benchmark.json'))
            benchmark.save_benchmark_report(path, report)
            self.assertEqual(report, benchmark.load_resumable_benchmark_report(path, manifest, prior_env))
            with self.assertRaisesRegex(ValueError, 'environment changed'):
                benchmark.load_resumable_benchmark_report(path, manifest, new_env)


if __name__ == '__main__':unittest.main()
