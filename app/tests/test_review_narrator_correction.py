"""Review attribution is editable while source text and metadata stay bound (#1951)."""
import copy
from contextlib import redirect_stdout
import io
import unittest

from generate_script import LLMGenParams
import review_script as review
from tests.test_pass_boundary_retry_regressions import client_for


class ReviewNarratorCorrectionTests(unittest.TestCase):
    source = [{'speaker': 'NARRATOR', 'text': 'Please wait beside the gate.',
               'instruct': 'Neutral.', 'spoken': False, 'type': 'NARRATOR',
               'source_span': [5, 31], 'pause_after': 700,
               'provenance': {'section': [1, 2]}}]

    def run_review(self, answer, labels_only=False, literal=False):
        original = copy.deepcopy(self.source)
        client, calls = client_for([(answer, 'stop')])
        attempts = []
        with redirect_stdout(io.StringIO()):
            output = review.review_batch(client, 'fixture', original, 1, 1,
                LLMGenParams(review_labels_only=labels_only,
                             literal_source_preserving=literal),
                max_retries=0, attempt_observer=attempts.append)
        self.assertEqual(self.source, original)
        return output, calls, attempts

    def assert_corrected(self, output):
        self.assertEqual([{**{k: v for k, v in self.source[0].items() if k != 'spoken'},
                           'speaker': 'ALICE', 'instruct': 'Softly.'}], output)
        self.assertIsNot(self.source[0]['source_span'], output[0]['source_span'])
        self.assertIsNot(self.source[0]['provenance'], output[0]['provenance'])

    def test_classic_and_literal_accept_narrator_correction_without_model_metadata(self):
        answer = [{'speaker': 'ALICE', 'text': self.source[0]['text'], 'instruct': 'Softly.'}]
        for literal in (False, True):
            with self.subTest(literal=literal):
                output, calls, attempts = self.run_review(answer, literal=literal)
                self.assert_corrected(output)
                self.assertEqual(1, len(calls))
                self.assertEqual('accepted', attempts[0]['outcome'])
                self.assertIn(review.REVIEW_ATTRIBUTION_RULE,
                              calls[0]['messages'][-1]['content'])
                self.assertNotIn('Keep every portion of those spans assigned to NARRATOR',
                                 calls[0]['messages'][-1]['content'])

    def test_index_only_accepts_correction_without_text_or_metadata_from_model(self):
        output, calls, attempts = self.run_review(
            [{'n': 0, 'speaker': 'ALICE', 'instruct': 'Softly.'}], labels_only=True)
        self.assert_corrected(output)
        self.assertEqual('accepted', attempts[0]['outcome'])
        request = '\n'.join(message['content'] for message in calls[0]['messages'])
        self.assertIn(review.REVIEW_ATTRIBUTION_RULE, calls[0]['messages'][0]['content'])
        self.assertEqual(1, request.count(review.REVIEW_ATTRIBUTION_RULE))
        self.assertIn('Only return the fields required by the response format.', request)
        self.assertNotIn('Preserve source text, order and metadata.', request)
        self.assertNotIn('must remain NARRATOR', calls[0]['messages'][0]['content'])

    def test_character_to_narrator_preserves_source_in_every_review_mode(self):
        source = [{**copy.deepcopy(self.source[0]), 'speaker': 'ALICE',
                   'text': 'The lantern flickered.', 'spoken': True, 'type': 'SPOKEN'}]
        before = copy.deepcopy(source)
        for labels_only, literal in ((False, False), (False, True), (True, False)):
            with self.subTest(labels_only=labels_only, literal=literal):
                answer = {'speaker': 'NARRATOR', 'instruct': 'Neutral.'}
                answer.update({'n': 0} if labels_only else {'text': source[0]['text']})
                client, calls = client_for([([answer], 'stop')])
                with redirect_stdout(io.StringIO()):
                    output = review.review_batch(client, 'fixture', source, 1, 1,
                        LLMGenParams(review_labels_only=labels_only,
                                     literal_source_preserving=literal), max_retries=0)
                self.assertEqual([{**{k: v for k, v in before[0].items() if k != 'spoken'},
                                   'speaker': 'NARRATOR'}], output)
                self.assertEqual(before, source)
                self.assertEqual(1, len(calls))
                self.assertIsNot(source[0]['provenance'], output[0]['provenance'])

    def test_punctuation_recovery_restores_source_while_accepting_narrator_correction(self):
        output, _, _ = self.run_review(
            [{'speaker': 'ALICE', 'text': 'Please wait beside the gate!', 'instruct': 'Softly.'}])
        self.assert_corrected(output)

    def test_narrator_correction_still_rejects_word_loss_addition_and_reordering(self):
        for text in ('Please wait.', 'Please wait beside the gate. Now.',
                     'Beside the gate, please wait.'):
            with self.subTest(text=text):
                output, _, attempts = self.run_review(
                    [{'speaker': 'ALICE', 'text': text, 'instruct': 'Softly.'}])
                self.assertIsNone(output)
                self.assertEqual('quality_rejected', attempts[0]['outcome'])

    def test_review_labels_do_not_turn_quote_heuristics_into_attribution_locks(self):
        labels = [{'n': 0, 'speaker': 'ALICE', 'instruct': 'Softly.'}]
        for spoken in (True, False, None, 0, 1, 'false'):
            for kind in ('NARRATOR', 'SPOKEN', None):
                with self.subTest(spoken=spoken, kind=kind):
                    original = [{**copy.deepcopy(self.source[0]), 'spoken': spoken}]
                    if kind is None:
                        original[0].pop('type')
                    else:
                        original[0]['type'] = kind
                    before = copy.deepcopy(original)
                    _, quality = review.get_review_label_binding(original, labels)
                    self.assertTrue(quality['passed'], quality)
                    self.assertEqual(before, original)
