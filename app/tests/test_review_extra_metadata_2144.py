"""Issue #2144: review restores extra input metadata instead of dropping it.

The model response contract asks reviewers to omit input-only fields because the
runtime rebinds them from the original entries. These tests pin both halves of
that contract: the prompt says so explicitly (including unrecognized keys), and
the binding really preserves omitted/altered/spoofed extra keys across the
lossless split/merge paths.
"""
import copy
import io
import json
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from generate_script import LLMGenParams
from literal_policy import bind_literal_policy
import review_script as review
from tests import test_review_receipt_identity_boundary as receipt_tests
from tests.test_pass_boundary_retry_regressions import client_for


EXTRA = {
    'chapter_id': 'ch-3',
    'author_tag': 'EXTRA_TAG',
    'nested': {'flags': [True, 1, None]},
}


def source_rows():
    return [
        {'speaker': 'ALICE', 'text': 'The bell rang.', 'instruct': '',
         'source_span': [0, 14], 'provenance': {'section': 7}, 'pause_after': 125,
         **copy.deepcopy(EXTRA)},
        {'speaker': 'BOB', 'text': 'A second line.', 'instruct': '',
         'source_span': [16, 30], 'provenance': {'section': 7},
         **copy.deepcopy(EXTRA)},
    ]


def review_with(reply, source, *, literal=False, source_text=None):
    client, _calls = client_for([(reply, 'stop')])
    with redirect_stdout(io.StringIO()):
        return review.review_batch(
            client, 'fixture', source, 1, 1,
            LLMGenParams(literal_source_preserving=literal),
            max_retries=0, source_text=source_text)


class ReviewExtraMetadata2144Tests(unittest.TestCase):
    RAW = 'The bell rang.\n\nA second line.'

    def test_prompts_state_extra_keys_are_omitted_and_restored(self):
        prompts = (review.REVIEW_SYSTEM_PROMPT,
                   review.REVIEW_CLASSIC_RESPONSE_RULE)
        for prompt in prompts:
            self.assertIn('restored from the original entries', prompt)
            self.assertIn('does not remove them from the saved script', prompt)
            self.assertTrue('unrecognized' in prompt or 'extra keys' in prompt)

    def test_sent_prompt_requires_text_only_in_full_review_modes(self):
        source = source_rows()
        full_reply = [{key: row[key] for key in ('speaker', 'text', 'instruct')}
                      for row in source]
        labels_reply = [{'n': index, 'speaker': row['speaker'], 'instruct': row['instruct']}
                        for index, row in enumerate(source)]
        cases = (
            ('shipped', LLMGenParams(), full_reply),
            ('custom', LLMGenParams(system_prompt='Synthetic custom reviewer.',
                                    user_prompt_template='{context}\n{batch}'), full_reply),
            ('literal', LLMGenParams(literal_source_preserving=True), full_reply),
            ('labels-only', LLMGenParams(review_labels_only=True), labels_reply),
        )
        for mode, params, reply in cases:
            with self.subTest(mode=mode):
                client, calls = client_for([(reply, 'stop')])
                with redirect_stdout(io.StringIO()):
                    bound = review.review_batch(
                        client, 'fixture', source, 1, 1, params,
                        max_retries=0, source_text=self.RAW)
                self.assertEqual(len(source), len(bound))
                self.assertEqual(1, len(calls))
                system = next(message['content'] for message in calls[0]['messages']
                              if message['role'] == 'system')
                self.assertIn('all other input-only metadata keys', system)
                self.assertIn('Omit type to retain the source role', system)
                self.assertNotIn('Source text, source_span', system)
                if mode == 'labels-only':
                    self.assertIn('Never return text', system)
                    self.assertNotIn('Never omit speaker, text or instruct', system)
                else:
                    self.assertIn('Never omit speaker, text or instruct', system)
                    self.assertNotIn('Never return text', system)

    def test_omitted_altered_and_spoofed_extra_keys_are_restored(self):
        source = source_rows()
        minimal = [{'speaker': row['speaker'], 'text': row['text'],
                    'instruct': row['instruct']} for row in source]
        altered = [dict(row, chapter_id='WRONG', author_tag='FORGED', nested=False)
                   for row in minimal]
        spoofed = [dict(row, source_span=[-1, -1], provenance={'forged': True})
                   for row in altered]
        for literal in (False, True):
            for reply in (minimal, altered, spoofed):
                with self.subTest(literal=literal, reply=reply[0].get('chapter_id')):
                    before = copy.deepcopy(source)
                    result = review_with(reply, source, literal=literal,
                                         source_text=self.RAW)
                    self.assertIsNotNone(result)
                    for bound in result:
                        for key, value in EXTRA.items():
                            self.assertEqual(value, bound[key])
                        self.assertEqual({'section': 7}, bound['provenance'])
                    self.assertEqual(before, source)

    def test_valid_split_preserves_extra_metadata_on_each_fragment(self):
        source = source_rows()
        reply = [
            {'speaker': 'ALICE', 'text': 'The bell', 'instruct': 'Softly.'},
            {'speaker': 'ALICE', 'text': 'rang.', 'instruct': 'Softly.'},
            {'speaker': 'BOB', 'text': 'A second line.', 'instruct': ''},
        ]
        result = review_with(reply, source, source_text=self.RAW)
        self.assertIsNotNone(result)
        self.assertEqual(3, len(result))
        for bound in result:
            for key, value in EXTRA.items():
                self.assertEqual(value, bound[key])
        self.assertEqual([[0, 8], [9, 14], [16, 30]],
                         [entry['source_span'] for entry in result])

    def test_merge_without_span_preserves_extra_metadata(self):
        source = [
            {'speaker': 'NARRATOR', 'text': 'First.', 'instruct': 'Even.',
             **copy.deepcopy(EXTRA)},
            {'speaker': 'NARRATOR', 'text': 'Second.', 'instruct': 'Even.',
             **copy.deepcopy(EXTRA)},
        ]
        reply = [{'speaker': 'NARRATOR', 'text': 'First. Second.',
                  'instruct': 'Even.'}]
        result = review_with(reply, source)
        self.assertIsNotNone(result)
        self.assertEqual(1, len(result))
        self.assertEqual('First. Second.', result[0]['text'])
        for key, value in EXTRA.items():
            self.assertEqual(value, result[0][key])

    def test_unrecognized_reply_key_is_not_copied_into_bound_entry(self):
        source = source_rows()
        reply = [{'speaker': row['speaker'], 'text': row['text'],
                  'instruct': row['instruct'], 'model_invented': 'nope'}
                 for row in source]
        result = review_with(reply, source, source_text=self.RAW)
        self.assertIsNotNone(result)
        self.assertTrue(all('model_invented' not in entry for entry in result))


class ReviewContractReceiptTests(receipt_tests.Harness):
    def test_changed_response_contract_invalidates_custom_and_literal_receipts(self):
        self.config['prompts'] = {'review_system_prompt': 'Synthetic custom reviewer.',
                                  'review_user_prompt': '{context}\n{batch}'}
        for mode in ('custom', 'literal'):
            with self.subTest(mode=mode):
                script = self.root / f'{mode}.json'
                entries = self.rows
                if mode == 'literal':
                    raw = ' '.join(row['text'] for row in entries)
                    entries = bind_literal_policy(raw, entries)
                script.write_text(json.dumps(entries))
                self.calls.clear()
                old_contract = review.REVIEW_CLASSIC_RESPONSE_RULE + ' Previous contract.'
                with patch.object(review, 'REVIEW_CLASSIC_RESPONSE_RULE', old_contract):
                    self.cli(path=script)
                    self.assertEqual(len(entries), len(self.calls))
                    previous = self.receipt(script)['fingerprint']
                self.calls.clear()
                self.cli(path=script)
                self.assertEqual(len(entries), len(self.calls))
                self.assertNotEqual(previous, self.receipt(script)['fingerprint'])
                self.calls.clear()
                self.cli(path=script)
                self.assertEqual([], self.calls)


if __name__ == '__main__':
    unittest.main()
