"""Classic review restores source metadata without trusting re-emitted prose."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from generate_script import LLMGenParams
from literal_policy import bind_literal_policy, get_literal_policy
import review_script as review
from tests.test_pass_boundary_retry_regressions import client_for


def row(text, speaker='ALICE', **metadata):
    return {'speaker': speaker, 'text': text, 'instruct': '', **metadata}


class ReviewSourceBindingTests(unittest.TestCase):
    source = [row('And yet— Just turn your head.', speaker='BOB', pause_after=1500,
                  speaker_gender='female', speaker_age_group='young_adult',
                  speaker_ageless=True, type='SPOKEN', dialogue_provenance={'quote': [4, 9]}),
              row('said Alice.', speaker='NARRATOR')]

    def run_review(self, source, replies, **kwargs):
        client, calls = client_for([(reply, 'stop') for reply in replies])
        before = copy.deepcopy(source)
        attempts = []
        with patch('generate_script.time.sleep'), redirect_stdout(io.StringIO()):
            result = review.review_batch(client, 'fixture', source, 1, 1,
                kwargs.pop('params', LLMGenParams()), max_retries=len(replies) - 1,
                attempt_observer=attempts.append, **kwargs)
        self.assertEqual(before, source)
        return result, calls, attempts

    def test_three_field_reply_preserves_all_metadata_and_original_whitespace(self):
        source = [{**self.source[0], 'text': '  And yet—\nJust turn your head.  '}, self.source[1]]
        reply = [row('And yet— Just turn your head.', speaker='ALICE'), self.source[1]]
        output, calls, attempts = self.run_review(source, [reply])
        self.assertEqual([{**source[0], 'speaker': 'ALICE'}, source[1]], output)
        self.assertIsNot(source[0]['dialogue_provenance'], output[0]['dialogue_provenance'])
        self.assertEqual(1, len(calls))
        self.assertEqual('accepted', attempts[0]['outcome'])

    def test_model_metadata_cannot_overwrite_source_or_invent_new_fields(self):
        reply = [{**self.source[0], 'pause_after': 0, 'speaker_gender': 'male',
                  'dialogue_provenance': {}, 'invented_metadata': True}, self.source[1]]
        output, _, _ = self.run_review(self.source, [reply])
        self.assertEqual(self.source, output)

    def test_punctuation_drift_recovers_labels_but_preserves_exact_source_text(self):
        reply = [row('And yet, Just turn your head.', speaker='ALICE'), self.source[1]]
        reply[0]['instruct'] = 'Gently.'
        output, calls, _ = self.run_review(self.source, [reply])
        self.assertEqual('ALICE', output[0]['speaker'])
        self.assertEqual('Gently.', output[0]['instruct'])
        self.assertEqual([{**self.source[0], 'speaker': 'ALICE', 'instruct': 'Gently.'},
                          self.source[1]], output)
        self.assertEqual(1, len(calls))

    def test_lexical_edits_reorders_duplicate_rows_and_missing_rows_retry(self):
        good = [row(self.source[0]['text'], speaker='ALICE'), self.source[1]]
        invalid = ([row('And yet— Just turn your face.', speaker='ALICE'), self.source[1]],
                   good[::-1], [good[0], good[0]], good[:1], good + [good[0]])
        for reply in invalid:
            with self.subTest(reply=reply):
                output, calls, attempts = self.run_review(self.source, [reply, good])
                self.assertEqual(2, len(calls))
                self.assertEqual('quality_rejected', attempts[0]['outcome'])
                self.assertIn('review_text_mismatch', attempts[0]['failure_codes'])
                self.assertEqual('ALICE', output[0]['speaker'])
                self.assertEqual(self.source[0]['text'], output[0]['text'])

    def test_exhausted_unsafe_reply_returns_none_without_partial_label_application(self):
        reply = [row('And yet— Just turn your face.', speaker='ALICE'), self.source[1]]
        output, _, attempts = self.run_review(self.source, [reply])
        self.assertIsNone(output)
        self.assertEqual('quality_rejected', attempts[0]['outcome'])

    def test_ambiguous_repeated_word_sequences_reject_punctuation_recovery(self):
        source = [row('Yes!', speaker='BOB', pause_after=100), row('Yes?', speaker='ALICE')]
        reply = [row('Yes.', speaker='ALICE'), row('Yes?', speaker='BOB')]
        output, _, _ = self.run_review(source, [reply])
        self.assertIsNone(output)

    def test_exact_repeated_phrases_keep_positional_metadata_without_false_rejection(self):
        source = [row('Yes.', speaker='BOB', pause_after=100),
                  row('Yes.', speaker='ALICE', pause_after=200)]
        reply = [row('Yes.', speaker='ALICE'), row('Yes.', speaker='BOB')]
        output, _, _ = self.run_review(source, [reply])
        self.assertEqual([100, 200], [entry['pause_after'] for entry in output])
        self.assertEqual(['ALICE', 'BOB'], [entry['speaker'] for entry in output])

    def test_symbol_changes_case_changes_and_contraction_changes_are_not_punctuation_recovery(self):
        for source_text, reply_text in (('It costs $5.', 'It costs 5.'),
                                       ('Compute 5+6.', 'Compute 5/6.'),
                                       ("Don't stop.", 'Dont stop.'),
                                       ('Alice speaks.', 'alice speaks.'),
                                       ('Go 🐈.', 'Go 🐕.')):
            with self.subTest(source=source_text):
                output, _, _ = self.run_review([row(source_text)], [[row(reply_text)]])
                self.assertIsNone(output)

    def test_punctuation_recovery_reuses_narrator_guard_and_labels_validation(self):
        source = [row('Alice answered— quietly.', speaker='NARRATOR', type='NARRATOR')]
        for reply in ([row('Alice answered, quietly.', speaker='ALICE')],
                      [row('Alice answered, quietly.', speaker='')],
                      [{**row('Alice answered, quietly.'), 'instruct': None}]):
            with self.subTest(reply=reply):
                output, _, _ = self.run_review(source, [reply])
                self.assertIsNone(output)

    def test_punctuation_recovery_uses_source_speech_over_wrong_model_narration(self):
        for kind in (None, 'NARRATOR'):
            with self.subTest(kind=kind):
                source = [row('Please wait— beside the gate.', speaker='NARRATOR', spoken=True,
                              source_span=[4, 37], pause_after=700)]
                if kind is not None:
                    source[0]['type'] = kind
                reply = [row('Please wait, beside the gate.', speaker='ALICE')]
                output, calls, _ = self.run_review(source, [reply])
                self.assertEqual([{**source[0], 'speaker': 'ALICE'}], output)
                self.assertIsNot(source[0]['source_span'], output[0]['source_span'])
                self.assertEqual(1, len(calls))

    def test_punctuation_recovery_protects_source_narration_despite_model_speech(self):
        source = [row('Alice waited— beside the gate.', speaker='BOB', spoken=False, type='SPOKEN')]
        reply = [row('Alice waited, beside the gate.', speaker='ALICE')]
        output, calls, attempts = self.run_review(source, [reply, reply])
        self.assertIsNone(output)
        self.assertEqual(2, len(calls))
        self.assertTrue(all('narrator_renamed' in attempt['failure_codes'] for attempt in attempts))

    def test_lossless_single_source_split_copies_metadata_and_pauses_only_at_source_end(self):
        source = [row('Hello, said Alice; goodbye.', pause_after=1500,
                      speaker_gender='female', speaker_age_group='young_adult',
                      speaker_ageless=False, provenance={'quote': [0, 26]})]
        reply = [row('Hello,'), row('said Alice;', speaker='NARRATOR'), row('goodbye.')]
        output, _, _ = self.run_review(source, [reply])
        self.assertEqual([None, None, 1500], [entry.get('pause_after') for entry in output])
        for entry in output:
            self.assertEqual(source[0]['provenance'], entry['provenance'])
            self.assertIsNot(source[0]['provenance'], entry['provenance'])
            self.assertEqual('female', entry['speaker_gender'])
        self.assertEqual(' '.join(entry['text'] for entry in source),
                         ' '.join(entry['text'] for entry in output))

    def test_lossless_merge_preserves_shared_metadata_and_final_pause(self):
        source = [row('First phrase.', provenance={'section': 1}),
                  row('Second phrase.', provenance={'section': 1}, pause_after=700)]
        reply = [row('First phrase. Second phrase.')]
        output, _, _ = self.run_review(source, [reply])
        self.assertEqual([{**reply[0], 'provenance': {'section': 1}, 'pause_after': 700}], output)

    def test_lossless_merge_rejects_metadata_conflicts_or_an_interior_pause(self):
        for source, code in (([row('First phrase.', provenance={'section': 1}),
                              row('Second phrase.', provenance={'section': 2})], 'review_metadata_conflict'),
                             ([row('First phrase.', speaker_gender='female'),
                               row('Second phrase.')], 'review_metadata_conflict'),
                             ([row('First phrase.', pause_after=700),
                               row('Second phrase.')], 'review_pause_boundary')):
            with self.subTest(code=code):
                output, _, attempts = self.run_review(source, [[row('First phrase. Second phrase.')]])
                self.assertIsNone(output)
                self.assertIn(code, attempts[0]['failure_codes'])

    def test_changed_boundaries_with_equal_count_do_not_borrow_metadata(self):
        source = [row('First phrase. Next words.', provenance={'section': 1}),
                  row('Last phrase.', provenance={'section': 2})]
        reply = [row('First phrase.'), row('Next words. Last phrase.')]
        output, _, attempts = self.run_review(source, [reply])
        self.assertIsNone(output)
        self.assertIn('review_metadata_conflict', attempts[0]['failure_codes'])

    def test_literal_punctuation_recovery_keeps_policy_and_does_not_accept_unverifiable_words(self):
        source = bind_literal_policy(self.source[0]['text'], [self.source[0]])
        output, _, _ = self.run_review(source, [[row('And yet, Just turn your head.', speaker='ALICE')]])
        self.assertEqual(source[0]['text'], output[0]['text'])
        self.assertEqual(get_literal_policy(source), get_literal_policy(output))
        for text in ('...', '\u0301Words.'):
            with self.subTest(text=text):
                output, _, attempts = self.run_review([row(text)], [[row(text)]],
                    params=LLMGenParams(literal_source_preserving=True))
                self.assertIsNone(output)
                self.assertIn('literal_source_word_mismatch', attempts[0]['failure_codes'])

    def test_adaptive_split_recombination_restores_original_pause_boundary(self):
        source = [row('Hello. Goodbye.', pause_after=1500, provenance={'section': 1})]
        client, calls = client_for([(source, 'length'), ([row('Hello.')], 'stop'),
                                    ([row('Goodbye.')], 'stop')])
        client.switched = True
        with patch('generate_script.split_failed_chunk', return_value=['Hello.', 'Goodbye.']), \
             redirect_stdout(io.StringIO()):
            output = review.review_batch(client, 'fixture', source, 1, 1,
                LLMGenParams(max_tokens=100, hard_max_tokens=100), max_retries=0)
        self.assertEqual(3, len(calls))
        self.assertEqual([None, 1500], [entry.get('pause_after') for entry in output])
        self.assertEqual([{'section': 1}] * 2, [entry['provenance'] for entry in output])

    def test_real_cli_publication_keeps_pause_and_traits_in_the_saved_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'fixture.json')
            source.write_text(json.dumps(self.source))
            reply = [row('And yet, Just turn your head.', speaker='ALICE'), self.source[1]]
            client, calls = client_for([(reply, 'stop')])
            config = {'llm_mode': 'remote', 'llm_remote': {'model_name': 'fixture'},
                      'generation': {'review_batch_size': 25}}
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(review.sys, 'argv', ['review', '--input', str(source)]))
                for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                    ('ensure_ideal_settings', (True, {}, 'fixture')),
                                    ('get_active_llm_config', config['llm_remote']),
                                    ('make_run_client', client), ('get_cached_or_benchmarked_concurrency', 1),
                                    ('get_current_status', {'loaded': False})):
                    stack.enter_context(patch.object(review, name, return_value=value))
                stack.enter_context(patch.object(review, 'wait_for_vram_headroom', side_effect=AssertionError('No GPU calls')))
                review.main()
            saved = json.loads(source.read_text())
            self.assertEqual([{**self.source[0], 'speaker': 'ALICE'}, self.source[1]], saved)
            self.assertEqual(1, len(calls))
            self.assertFalse(Path(str(source) + '.review_checkpoint.json').exists())
