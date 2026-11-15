"""Classic review restores source metadata without trusting re-emitted prose."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dialogue_spans import mark_entries
from generate_script import LLMGenParams
from literal_policy import bind_literal_policy, get_literal_policy
import review_script as review
from tests.test_pass_boundary_retry_regressions import client_for


def row(text, speaker='ALICE', **metadata):
    return {'speaker': speaker, 'text': text, 'instruct': '', **metadata}


def unresolved_row(entry, **labels):
    # Unknown corrected speakers must not inherit the old speaker's annotations.
    return {**{key: value for key, value in entry.items()
               if key not in ('spoken', 'speaker_gender', 'speaker_age_group', 'speaker_ageless')},
            **labels}


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

    def test_classic_response_contract_reaches_default_custom_and_literal_requests(self):
        for custom, literal in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(custom=custom, literal=literal):
                params = LLMGenParams(literal_source_preserving=literal,
                    system_prompt='Custom reviewer.' if custom else None,
                    user_prompt_template='{batch}' if custom else None)
                output, calls, _ = self.run_review([row('The bell rang.')],
                    [[row('The bell rang.')]], params=params)
                self.assertIsNotNone(output)
                system = calls[0]['messages'][0]['content']
                self.assertIn('REVIEW RESPONSE CONTRACT', system)
                self.assertIn('speaker, text and instruct', system)
                self.assertIn('source_span', system)
                self.assertIn('restored from the original entries', system)

    def test_shipped_prompt_distinguishes_response_fields_from_input_metadata(self):
        self.assertNotIn('same format as input', review.REVIEW_SYSTEM_PROMPT)
        self.assertIn('source_span', review.REVIEW_SYSTEM_PROMPT)
        self.assertIn('restored from the original entries', review.REVIEW_SYSTEM_PROMPT)

    def test_omitted_and_spoofed_protected_metadata_produce_the_same_source_bound_result(self):
        source = [row('A bell rang.', spoken=False, source_span=[0, 12]),
                  row('Please open the gate.', spoken=True, source_span=[14, 35],
                      speaker_gender='female', speaker_age_group='adult', speaker_ageless=False)]
        minimal = [row(entry['text'], speaker='NARRATOR' if i == 0 else 'ALICE')
                   for i, entry in enumerate(source)]
        spoofed = [{**entry, 'source_span': [-1, -1], 'provenance': {'untrusted': True}}
                   for i, entry in enumerate(minimal)]
        outputs = []
        for answer in (minimal, spoofed):
            output, _, attempts = self.run_review(source, [answer])
            self.assertEqual('accepted', attempts[0]['outcome'])
            self.assertEqual([unresolved_row(source[0], speaker='NARRATOR'), source[1]], output)
            outputs.append(output)
        self.assertEqual(*outputs)

    def test_missing_required_review_fields_retry_instead_of_being_restored(self):
        source = [row('The bell rang.')]
        for key in ('speaker', 'text', 'instruct'):
            with self.subTest(key=key):
                incomplete = [{k: v for k, v in source[0].items() if k != key}]
                output, calls, attempts = self.run_review(source, [incomplete, source])
                self.assertEqual(source, output)
                self.assertEqual(2, len(calls))
                self.assertEqual('quality_rejected', attempts[0]['outcome'])
                self.assertIn('missing_fields', attempts[0]['failure_codes'])

    def test_three_field_reply_preserves_all_metadata_and_original_whitespace(self):
        source = [{**self.source[0], 'text': '  And yet—\nJust turn your head.  '}, self.source[1]]
        reply = [row('And yet— Just turn your head.', speaker='ALICE'), self.source[1]]
        output, calls, attempts = self.run_review(source, [reply])
        self.assertEqual([unresolved_row(source[0], speaker='ALICE'), source[1]], output)
        self.assertIsNot(source[0]['dialogue_provenance'], output[0]['dialogue_provenance'])
        self.assertEqual(1, len(calls))
        self.assertEqual('accepted', attempts[0]['outcome'])

    def test_model_metadata_cannot_overwrite_source_or_invent_new_fields(self):
        reply = [{**self.source[0], 'pause_after': 0, 'speaker_gender': 'male',
                  'dialogue_provenance': {}, 'invented_metadata': True}, self.source[1]]
        output, _, _ = self.run_review(self.source, [reply])
        self.assertEqual([{**self.source[0], 'speaker_gender': 'male'}, self.source[1]], output)

    def test_punctuation_drift_recovers_labels_but_preserves_exact_source_text(self):
        reply = [row('And yet, Just turn your head.', speaker='ALICE'), self.source[1]]
        reply[0]['instruct'] = 'Gently.'
        output, calls, _ = self.run_review(self.source, [reply])
        self.assertEqual('ALICE', output[0]['speaker'])
        self.assertEqual('Gently.', output[0]['instruct'])
        self.assertEqual([unresolved_row(self.source[0], speaker='ALICE', instruct='Gently.'),
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

    def test_punctuation_recovery_keeps_labels_validation(self):
        source = [row('Alice answered— quietly.', speaker='NARRATOR', type='NARRATOR')]
        for reply in ([row('Alice answered, quietly.', speaker='')],
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
                self.assertEqual([unresolved_row(source[0], speaker='ALICE')], output)
                self.assertIsNot(source[0]['source_span'], output[0]['source_span'])
                self.assertEqual(1, len(calls))

    def test_punctuation_recovery_accepts_correction_and_keeps_quote_metadata(self):
        source = [row('Please wait— beside the gate.', speaker='NARRATOR', spoken=False, type='NARRATOR')]
        reply = [row('Please wait, beside the gate.', speaker='ALICE')]
        output, calls, attempts = self.run_review(source, [reply])
        self.assertEqual([unresolved_row(source[0], speaker='ALICE')], output)
        self.assertEqual(1, len(calls))
        self.assertEqual('accepted', attempts[0]['outcome'])

    def test_exact_full_review_accepts_correction_and_explicit_spoken_annotation(self):
        source = [row('Please wait beside the gate.', speaker='NARRATOR', spoken=False, type='NARRATOR',
                      source_span=[4, 30], pause_after=700)]
        reply = [row(source[0]['text'], speaker='ALICE', spoken=True)]
        output, calls, attempts = self.run_review(source, [reply])
        self.assertEqual(1, len(calls))
        self.assertEqual('accepted', attempts[0]['outcome'])
        self.assertEqual([{**unresolved_row(source[0], speaker='ALICE'), 'spoken': True}], output)
        self.assertIsNot(source[0]['source_span'], output[0]['source_span'])
        self.assertIn(review.REVIEW_ATTRIBUTION_RULE, calls[0]['messages'][-1]['content'])

    def test_full_review_split_and_merge_allow_label_corrections_with_metadata(self):
        split_source = [row('Rain fell. Wind rose.', speaker='NARRATOR', spoken=False,
                            provenance={'section': 1}, pause_after=700)]
        merge_source = [row('Rain fell.', speaker='NARRATOR', spoken=False, provenance={'section': 1}),
                        row('Wind rose.', speaker='NARRATOR', spoken=False,
                            provenance={'section': 1}, pause_after=700)]
        for source, texts in ((split_source, ['Rain fell.', 'Wind rose.']),
                              (merge_source, ['Rain fell. Wind rose.'])):
            with self.subTest(texts=texts):
                reply = [row(text, speaker='ALICE') for text in texts]
                output, calls, attempts = self.run_review(source, [reply])
                self.assertEqual(1, len(calls))
                self.assertEqual('accepted', attempts[0]['outcome'])
                self.assertEqual(texts, [entry['text'] for entry in output])
                self.assertTrue(all(entry['speaker'] == 'ALICE' and 'spoken' not in entry
                                    for entry in output))
                self.assertEqual([None] * (len(texts) - 1) + [700],
                                 [entry.get('pause_after') for entry in output])

    def test_full_review_preserves_speech_and_legacy_boundary_corrections(self):
        for spoken in (True, False, None, 0, 1, 'false', [], {}, Ellipsis):
            with self.subTest(spoken=spoken):
                source = [row('Hello. Rain fell.', speaker='NARRATOR', type='NARRATOR', pause_after=700)]
                if spoken is not Ellipsis:
                    source[0]['spoken'] = spoken
                reply = [row('Hello.', speaker='ALICE'), row('Rain fell.', speaker='NARRATOR')]
                output, calls, _ = self.run_review(source, [reply])
                self.assertEqual(['ALICE', 'NARRATOR'], [entry['speaker'] for entry in output])
                self.assertEqual([None, 700], [entry.get('pause_after') for entry in output])
                self.assertEqual(1, len(calls))
                self.assertNotIn('source-confirmed narration', calls[0]['messages'][-1]['content'])

    def test_full_review_keeps_positional_quote_metadata_for_repeated_text(self):
        source = [row('Wait.', speaker='NARRATOR', spoken=False, provenance={'quote': 1}),
                  row('Wait.', speaker='NARRATOR', spoken=True, provenance={'quote': 2})]
        reply = [row('Wait.', speaker='ALICE'), row('Wait.', speaker='ALICE')]
        output, calls, attempts = self.run_review(source, [reply])
        self.assertEqual(1, len(calls))
        self.assertEqual('accepted', attempts[0]['outcome'])
        self.assertEqual([{'quote': 1}, {'quote': 2}], [entry['provenance'] for entry in output])
        self.assertTrue(all('spoken' not in entry for entry in output))
        self.assertEqual(['ALICE', 'ALICE'], [entry['speaker'] for entry in output])

    def test_full_review_partitions_merges_to_preserve_boolean_role_facts(self):
        for spoken in ((0, False), (False, 0), (1, True), (True, 1)):
            with self.subTest(spoken=spoken):
                source = [row('Rain fell.', speaker='NARRATOR', spoken=spoken[0]),
                          row('Wind rose.', speaker='NARRATOR', spoken=spoken[1])]
                reply = [row('Rain fell. Wind rose.', speaker='NARRATOR')]
                output, _, attempts = self.run_review(source, [reply])
                self.assertEqual(source, output)
                self.assertEqual('accepted', attempts[0]['outcome'])
                for original, bound in zip(source, output):
                    self.assertIs(type(original['spoken']), type(bound['spoken']))
        source = [row('Rain fell.', spoken=0), row('Wind rose.', spoken=0)]
        output, _, _ = self.run_review(source, [[row('Rain fell. Wind rose.')]])
        self.assertEqual(0, output[0]['spoken'])
        self.assertIs(type(output[0]['spoken']), int)

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
            if entry['speaker'] == 'ALICE':
                self.assertEqual('female', entry['speaker_gender'])
            else:
                self.assertNotIn('speaker_gender', entry)
        self.assertEqual(' '.join(entry['text'] for entry in source),
                         ' '.join(entry['text'] for entry in output))

    def test_lossless_merge_preserves_shared_metadata_and_final_pause(self):
        source = [row('First phrase.', provenance={'section': 1}),
                  row('Second phrase.', provenance={'section': 1}, pause_after=700)]
        reply = [row('First phrase. Second phrase.')]
        output, _, _ = self.run_review(source, [reply])
        self.assertEqual([{**reply[0], 'provenance': {'section': 1}, 'pause_after': 700}], output)

    def test_compatible_merge_keeps_review_whitespace_and_bound_source_facts(self):
        source = [row('First phrase.', provenance={'section': 1}),
                  row('Second phrase.', provenance={'section': 1}, pause_after=700)]
        for separator in ('   ', '\t', '\n  '):
            with self.subTest(separator=separator):
                reply = [row('First phrase.' + separator + 'Second phrase.', speaker='NARRATOR')]
                before = copy.deepcopy(reply)
                output, _, attempts = self.run_review(source, [reply])
                self.assertEqual([{**reply[0], 'provenance': {'section': 1},
                                   'pause_after': 700}], output)
                self.assertEqual(before, reply)
                self.assertEqual('accepted', attempts[0]['outcome'])

    def test_lossless_merge_preserves_conflicting_metadata_and_interior_pauses_by_partitioning(self):
        for source in ([row('First phrase.', provenance={'section': 1}),
                        row('Second phrase.', provenance={'section': 2})],
                       [row('First phrase.', speaker_gender='female'), row('Second phrase.')],
                       [row('First phrase.', pause_after=700), row('Second phrase.')],
                       [row('First phrase.', pause_after=0), row('Second phrase.')]):
            with self.subTest(source=source):
                output, _, attempts = self.run_review(source, [[row('First phrase. Second phrase.')]])
                self.assertEqual(source, output)
                self.assertEqual('accepted', attempts[0]['outcome'])

    def test_changed_boundaries_with_equal_count_do_not_borrow_metadata(self):
        source = [row('First phrase. Next words.', provenance={'section': 1}),
                  row('Last phrase.', provenance={'section': 2})]
        reply = [row('First phrase.'), row('Next words. Last phrase.')]
        output, _, attempts = self.run_review(source, [reply])
        self.assertEqual([row('First phrase.', provenance={'section': 1}),
                          row('Next words.', provenance={'section': 1}),
                          row('Last phrase.', provenance={'section': 2})], output)
        self.assertEqual('accepted', attempts[0]['outcome'])

    def test_portion_reassignment_crossing_source_metadata_works_in_both_directions(self):
        for before, after in (('NARRATOR', 'ALICE'), ('ALICE', 'NARRATOR')):
            for literal in (False, True):
                with self.subTest(before=before, after=after, literal=literal):
                    source = [row('Rain fell. Please open the gate.', speaker=before,
                                  spoken=False, source_span=[0, 32], pause_after=700),
                              row('Take the blue cup.', speaker=after, spoken=True,
                                  source_span=[34, 52], speaker_gender='female',
                                  speaker_age_group='adult', speaker_ageless=False, pause_after=0)]
                    reply = [row('Rain fell.', speaker=before),
                             row('Please open the gate. Take the blue cup.', speaker=after)]
                    output, calls, attempts = self.run_review(source, [reply],
                        params=LLMGenParams(literal_source_preserving=literal))
                    first = {key: value for key, value in source[0].items() if key != 'pause_after'}
                    self.assertEqual([{**first, 'text': 'Rain fell.'},
                                      {**source[0], 'text': 'Please open the gate.', 'speaker': after,
                                       **{key: source[1][key] for key in ('spoken', 'speaker_gender',
                                            'speaker_age_group', 'speaker_ageless')}},
                                      source[1]], output)
                    self.assertNotIn('pause_after', output[0])
                    self.assertEqual([700, 0], [entry['pause_after'] for entry in output[1:]])
                    self.assertEqual(1, len(calls))
                    self.assertEqual('accepted', attempts[0]['outcome'])

    def test_portion_reassignment_remaps_source_spans_to_each_fragment(self):
        source_text = 'Rain fell. Please open the gate.\n\nTake the blue cup.'
        source = [row('Rain fell. Please open the gate.', speaker='NARRATOR',
                      spoken=False, provenance={'metadata': 'X'},
                      source_span=[0, 32], pause_after=700),
                  row('Take the blue cup.', spoken=True, provenance={'metadata': 'Y'},
                      source_span=[34, 52], speaker_gender='female', pause_after=0)]
        reply = [row('Rain fell.', speaker='NARRATOR'),
                 row('Please open the gate. Take the blue cup.')]
        before = copy.deepcopy(source)
        for literal in (False, True):
            with self.subTest(literal=literal):
                client, calls = client_for([(reply, 'stop')])
                attempts = []
                with redirect_stdout(io.StringIO()):
                    output = review.review_batch(client, 'fixture', source, 1, 1,
                        LLMGenParams(literal_source_preserving=literal), max_retries=0,
                        attempt_observer=attempts.append)
                self.assertEqual(before, source)
                self.assertEqual(1, len(calls))
                self.assertEqual('accepted', attempts[0]['outcome'])
                self.assertEqual(['Rain fell.', 'Please open the gate.', 'Take the blue cup.'],
                                 [entry['text'] for entry in output])
                self.assertEqual(['NARRATOR', 'ALICE', 'ALICE'],
                                 [entry['speaker'] for entry in output])
                self.assertEqual(['X', 'X', 'Y'],
                                 [entry['provenance']['metadata'] for entry in output])
                self.assertEqual([False, True, True], [entry['spoken'] for entry in output])
                self.assertNotIn('pause_after', output[0])
                self.assertEqual([700, 0], [entry['pause_after'] for entry in output[1:]])
                self.assertEqual([[0, 10], [11, 32], [34, 52]],
                                 [entry['source_span'] for entry in output],
                                 json.dumps(output, ensure_ascii=False, indent=2))
                for entry in output:
                    self.assertEqual(entry['text'], source_text[slice(*entry['source_span'])])
                for previous, current in zip(output, output[1:]):
                    self.assertLessEqual(previous['source_span'][1], current['source_span'][0])

    def test_raw_source_split_offsets_are_not_recoverable_from_whole_entry_spans(self):
        source = [row('Rain fell. Please  open the gate.', speaker='NARRATOR'),
                  row('Take the blue cup.')]
        fragments = [row('Rain fell.', speaker='NARRATOR'),
                     row('Please open the gate.'), row('Take the blue cup.')]
        raw_sources = ['Rain  fell. Please open the gate.\n\nTake the blue cup.',
                       'Rain fell. Please  open the gate.\n\nTake the blue cup.']
        originals = [mark_entries(source, raw) for raw in raw_sources]
        self.assertEqual(originals[0], originals[1])
        self.assertEqual([[0, 33], [35, 53]],
                         [entry['source_span'] for entry in originals[0]])
        self.assertEqual(len(source[0]['text']),
                         originals[0][0]['source_span'][1] - originals[0][0]['source_span'][0])
        mapped = [[entry['source_span'] for entry in mark_entries(fragments, raw)]
                  for raw in raw_sources]
        self.assertEqual([[[0, 11], [12, 33], [35, 53]],
                          [[0, 10], [11, 33], [35, 53]]], mapped)

    def test_partitioning_preserves_nested_json_types_that_compare_equal_in_python(self):
        source = [row('First phrase.', provenance={'flag': False}),
                  row('Second phrase.', provenance={'flag': 0})]
        output, _, _ = self.run_review(source, [[row('First phrase. Second phrase.')]])
        self.assertEqual(source, output)
        self.assertIs(type(output[0]['provenance']['flag']), bool)
        self.assertIs(type(output[1]['provenance']['flag']), int)

    def test_optional_narrator_merging_cannot_undo_nested_metadata_protection(self):
        source = [row('Rain fell.', speaker='NARRATOR', spoken=True, provenance={'flag': False}),
                  row('Wind rose.', speaker='NARRATOR', spoken=True, provenance={'flag': 0})]
        output, _, _ = self.run_review(source, [[row('Rain fell. Wind rose.', speaker='NARRATOR')]])
        merged, count = review.merge_consecutive_narrators(output)
        self.assertEqual(source, merged)
        self.assertEqual(0, count)
        self.assertIs(type(merged[0]['provenance']['flag']), bool)
        self.assertIs(type(merged[1]['provenance']['flag']), int)

    def test_only_incompatible_boundaries_partition_otherwise_compatible_neighbours(self):
        source = [row('First phrase.', provenance={'section': 1}),
                  row('Second phrase.', provenance={'section': 1}),
                  row('Third phrase.', provenance={'section': 2})]
        output, _, _ = self.run_review(source, [[row('First phrase. Second phrase. Third phrase.')]])
        self.assertEqual([row('First phrase. Second phrase.', provenance={'section': 1}),
                          source[2]], output)

    def test_reassigned_portion_uses_the_corrected_speakers_overlapping_attributes(self):
        traits = dict(speaker_gender='male', speaker_age_group='young_adult',
                      speaker_ageless=False, spoken=True)
        source = [row('A first portion.', speaker='NARRATOR', spoken=False,
                      source_span=[0, 16], provenance={'section': 1}, pause_after=100),
                  row('A second portion.', speaker='BOB', source_span=[18, 35], **traits)]
        for speaker, expected_traits in (('BOB', traits), ('NARRATOR', {'spoken': False})):
            with self.subTest(speaker=speaker):
                reply = [row('A first portion. A second portion.', speaker=speaker,
                             provenance={'spoofed': True})]
                output, calls, _ = self.run_review(source, [reply])
                self.assertEqual(1, len(calls))
                self.assertEqual([speaker, speaker], [entry['speaker'] for entry in output])
                for original, entry in zip(source, output):
                    self.assertEqual(expected_traits, {key: value for key, value in entry.items()
                        if key in ('speaker_gender', 'speaker_age_group', 'speaker_ageless', 'spoken')})
                    self.assertEqual(original['source_span'], entry['source_span'])
                    self.assertEqual(original.get('pause_after'), entry.get('pause_after'))
                    self.assertEqual(original.get('provenance'), entry.get('provenance'))
                rebound, report = review.get_review_source_binding(source, output)
                self.assertTrue(report['passed'])
                self.assertEqual(output, rebound)

    def test_known_speaker_attributes_follow_index_and_punctuation_label_corrections(self):
        traits = dict(speaker_gender='male', speaker_age_group='young_adult',
                      speaker_ageless=False, spoken=True)
        source = [row('First portion!', speaker='NARRATOR', spoken=False, source_span=[0, 14]),
                  row('Second portion.', speaker='BOB', **traits)]
        for labels_only, punctuation in ((True, False), (False, True)):
            with self.subTest(labels_only=labels_only, punctuation=punctuation):
                reply = ([{'n': i, 'speaker': 'BOB', 'instruct': ''} for i in range(2)]
                         if labels_only else [row('First portion.', speaker='BOB'), source[1]])
                output, _, _ = self.run_review(source, [reply],
                    params=LLMGenParams(review_labels_only=labels_only))
                self.assertEqual(source[0]['text'], output[0]['text'])
                self.assertEqual(source[0]['source_span'], output[0]['source_span'])
                self.assertEqual(traits, {key: output[0][key] for key in traits})

    def test_unknown_or_conflicting_speaker_annotations_clear_only_speaker_fields(self):
        original = row('First portion.', speaker='NARRATOR', spoken=False,
                       speaker_gender='female', speaker_age_group='adult', speaker_ageless=True,
                       type='NARRATOR', source_span=[0, 14], pause_after=0,
                       provenance={'flag': False})
        # Missing, disagreeing, and differently typed states must never select a donor.
        for donors in ([], [row('Second.', speaker='BOB', spoken=True),
                            row('Third.', speaker='BOB', spoken=False)],
                       [row('Second.', speaker='BOB', spoken=False),
                        row('Third.', speaker='BOB', spoken=0)]):
            with self.subTest(donors=donors):
                source = [original, *donors]
                reply = [row(original['text'], speaker='BOB'),
                         *[{key: donor[key] for key in ('speaker', 'text', 'instruct')} for donor in donors]]
                before = copy.deepcopy(source)
                output, quality = review.get_review_source_binding(source, reply)
                self.assertTrue(quality['passed'])
                self.assertEqual(unresolved_row(original, speaker='BOB'), output[0])
                self.assertEqual('review_speaker_metadata_unresolved', quality['warnings'][0]['code'])
                self.assertEqual(before, source)

    def test_annotationless_donor_warns_in_classic_and_index_review(self):
        source = [row('First.', speaker='NARRATOR', spoken=False, speaker_gender='female'),
                  row('Second.', speaker='BOB')]
        for labels_only in (False, True):
            with self.subTest(labels_only=labels_only):
                reply = ([{'n': i, 'speaker': 'BOB', 'instruct': ''} for i in range(2)]
                         if labels_only else [row('First.', speaker='BOB'), source[1]])
                output, _, attempts = self.run_review(source, [reply],
                    params=LLMGenParams(review_labels_only=labels_only))
                self.assertEqual(unresolved_row(source[0], speaker='BOB'), output[0])
                if labels_only:
                    ordered, quality = review.get_review_label_binding(source, reply)
                    _, warnings = review.get_review_labelled_entries(source, ordered)
                else:
                    _, quality = review.get_review_source_binding(source, reply)
                    warnings = quality['warnings']
                self.assertEqual(1, len(warnings))
                self.assertEqual('review_speaker_metadata_unresolved', warnings[0]['code'])
                self.assertEqual(1, warnings[0]['entry_number'])

    def test_annotationless_correction_does_not_claim_attributes_were_cleared(self):
        source = [row('First.', speaker='NARRATOR'), row('Second.', speaker='BOB')]
        reply = [row('First.', speaker='BOB'), source[1]]
        output, quality = review.get_review_source_binding(source, reply)
        self.assertEqual(reply, output)
        self.assertEqual([], quality['warnings'])

    def test_partitioned_partial_fragments_preserve_internal_review_whitespace(self):
        for space in ('  ', '\t', '\n  '):
            with self.subTest(space=space):
                source = [row('Hello there. Please open the gate.', source_span=[0, 33]),
                          row('Take the blue cup.', speaker='BOB', source_span=[35, 53])]
                reply = [row('Hello there.'),
                         row('Please' + space + 'open the gate. Take the blue', speaker='BOB'),
                         row('cup.', speaker='BOB')]
                output, _, _ = self.run_review(source, [reply])
                self.assertEqual('Please' + space + 'open the gate.', output[1]['text'])
                self.assertEqual('Take the blue', output[2]['text'])
                self.assertEqual(source[0]['source_span'], output[1]['source_span'])

    def test_unchanged_speaker_retains_per_line_temporal_states(self):
        source = [row('First.', spoken=True, speaker_age_group='child'),
                  row('Second.', spoken=False, speaker_age_group='adult')]
        output, quality = review.get_review_source_binding(source, source)
        self.assertEqual(source, output)
        self.assertEqual([], quality['warnings'])

    def test_overlapping_target_state_takes_precedence_over_conflicting_batch_state(self):
        source = [row('First.', speaker='NARRATOR', spoken=False, source_span=[0, 6]),
                  row('Second.', speaker='BOB', spoken=True, speaker_age_group='child'),
                  row('Third.', speaker='BOB', spoken=False, speaker_age_group='adult')]
        reply = [row('First. Second.', speaker='BOB'), source[2]]
        output, quality = review.get_review_source_binding(source, reply)
        self.assertEqual({**source[0], 'speaker': 'BOB', 'spoken': True,
                          'speaker_age_group': 'child'}, output[0])
        self.assertEqual(source[2], output[-1])
        self.assertEqual([], quality['warnings'])

    def test_partial_retention_rebinds_speaker_annotations_and_reports_unknowns(self):
        source = [row('First.', speaker='NARRATOR', spoken=False, source_span=[0, 6]),
                  row('Second.', speaker='BOB', spoken=True, speaker_gender='male'),
                  row('Third.', speaker='NARRATOR', spoken=False)]
        for target in ('BOB', 'UNKNOWN PERSON'):
            with self.subTest(target=target):
                reply = [row('First.', speaker=target), source[1]]
                output, status = review.get_review_partial_label_retention(source, reply, False)
                self.assertEqual('partial', status['status'])
                self.assertEqual([0], status['retained_positions'])
                self.assertEqual(source[0]['source_span'], output[0]['source_span'])
                self.assertEqual(source[2], output[2])
                if target == 'BOB':
                    self.assertIs(True, output[0]['spoken'])
                    self.assertEqual('male', output[0]['speaker_gender'])
                    self.assertEqual([], status['warnings'])
                else:
                    self.assertNotIn('spoken', output[0])
                    self.assertEqual('review_speaker_metadata_unresolved', status['warnings'][0]['code'])

    def test_adaptive_split_rebinds_annotations_against_original_batch(self):
        source = [row('First.', speaker='NARRATOR', spoken=False, pause_after=700),
                  row('Second.', speaker='BOB', spoken=True, speaker_gender='male')]
        for labels_only in (False, True):
            with self.subTest(labels_only=labels_only):
                answers = ([[{'n': 0, 'speaker': 'BOB', 'instruct': ''}]] * 2 if labels_only
                           else [[row('First.', speaker='BOB')], [source[1]]])
                client, calls = client_for([(source, 'length'), *( (a, 'stop') for a in answers)])
                client.switched = True
                with redirect_stdout(io.StringIO()):
                    output = review.review_batch(client, 'fixture', source, 1, 1,
                        LLMGenParams(review_labels_only=labels_only, max_tokens=100,
                                     hard_max_tokens=100), max_retries=0)
                self.assertEqual(3, len(calls))
                self.assertEqual([{**source[0], 'speaker': 'BOB', 'spoken': True,
                                   'speaker_gender': 'male'}, source[1]], output)

    def test_generated_boundary_overlays_preserve_word_labels_metadata_pauses_and_inputs(self):
        words = ['Alpha', '猫', 'café', 'delta', 'echo', 'foxtrot']
        def partitions(mask):
            start = 0
            for end in range(1, len(words) + 1):
                if end == len(words) or mask & (1 << (end - 1)):
                    yield words[start:end]
                    start = end
        for source_mask in range(1 << (len(words) - 1)):
            source, owner, pauses, cursor = [], {}, [], 0
            for i, part in enumerate(partitions(source_mask)):
                metadata = {'source_span': [cursor, cursor + len(part)],
                            'provenance': {'owner': i, 'flags': [False, 0, True, 1]}}
                entry = row(' \n '.join(part), **metadata)
                cursor += len(part)
                if i % 2 == 0:
                    entry['pause_after'] = i * 100
                    pauses.append((cursor, i * 100))
                source.append(entry)
                owner.update({word: metadata for word in part})
            for reply_mask in range(1 << (len(words) - 1)):
                with self.subTest(source_mask=source_mask, reply_mask=reply_mask):
                    reply, labels = [], {}
                    for i, part in enumerate(partitions(reply_mask)):
                        speaker = 'ALICE' if i % 2 else 'NARRATOR'
                        instruction = f'Direction {i}.'
                        reply.append({**row(' '.join(part), speaker=speaker,
                                            source_span=[-1, -1], provenance={'untrusted': True}),
                                      'instruct': instruction})
                        labels.update({word: (speaker, instruction) for word in part})
                    before_source, before_reply = copy.deepcopy(source), copy.deepcopy(reply)
                    output, quality = review.get_review_source_binding(source, reply)
                    self.assertTrue(quality['passed'], quality)
                    self.assertEqual(source, before_source)
                    self.assertEqual(reply, before_reply)
                    self.assertEqual(words, [word for entry in output for word in entry['text'].split()])
                    actual_pauses, cursor = [], 0
                    for entry in output:
                        for word in entry['text'].split():
                            self.assertEqual(labels[word], (entry['speaker'], entry['instruct']))
                            self.assertEqual(owner[word], {key: value for key, value in entry.items()
                                if key not in ('speaker', 'text', 'instruct', 'pause_after')})
                        cursor += len(entry['text'].split())
                        if 'pause_after' in entry:
                            actual_pauses.append((cursor, entry['pause_after']))
                    self.assertEqual(pauses, actual_pauses)
                    rebound, report = review.get_review_source_binding(source, output)
                    self.assertTrue(report['passed'], report)
                    self.assertEqual(output, rebound)

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

    def test_real_cli_publication_saves_narrator_corrections_and_preserves_metadata(self):
        for labels_only in (False, True):
            with self.subTest(labels_only=labels_only), tempfile.TemporaryDirectory() as directory:
                original = copy.deepcopy(self.source)
                original[0].update(speaker='NARRATOR', type='NARRATOR', spoken=False,
                                   source_span=[0, 31], speaker_ageless=False)
                source = Path(directory, 'fixture.json')
                source.write_text(json.dumps(original), encoding='utf-8')
                if labels_only:
                    reply = [{'n': i, 'speaker': 'ALICE' if i == 0 else entry['speaker'],
                              'instruct': entry['instruct']} for i, entry in enumerate(original)]
                else:
                    reply = [row('And yet, Just turn your head.', speaker='ALICE'), original[1]]
                client, calls = client_for([(reply, 'stop')])
                config = {'llm_mode': 'remote', 'llm_remote': {'model_name': 'fixture'},
                          'generation': {'review_batch_size': 25}}
                argv = ['review', '--input', str(source)] + (['--labels-only'] if labels_only else [])
                with ExitStack() as stack:
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(review.sys, 'argv', argv))
                    for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                        ('ensure_ideal_settings', (True, {}, 'fixture')),
                                        ('get_active_llm_config', config['llm_remote']),
                                        ('make_run_client', client), ('get_cached_or_benchmarked_concurrency', 1),
                                        ('get_current_status', {'loaded': False})):
                        stack.enter_context(patch.object(review, name, return_value=value))
                    stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                                    side_effect=AssertionError('No GPU calls')))
                    review.main()
                saved = json.loads(source.read_text(encoding='utf-8'))
                self.assertEqual([unresolved_row(original[0], speaker='ALICE'), original[1]], saved)
                self.assertEqual(1, len(calls))
                self.assertFalse(Path(str(source) + '.review_checkpoint.json').exists())
                self.assertTrue(Path(str(source) + '.review_completed.json').exists())

    def test_cli_publishes_portion_reassignment_in_sequential_and_contextual_modes(self):
        for context_window, nested_narrators in ((0, False), (1, False), (0, True), (1, True)):
            with self.subTest(context_window=context_window, nested_narrators=nested_narrators), \
                    tempfile.TemporaryDirectory() as directory:
                original = [row('Rain fell. Please open the gate.', speaker='NARRATOR',
                                spoken=False, source_span=[0, 32], pause_after=700),
                            row('Take the blue cup.', spoken=True, source_span=[34, 52],
                                speaker_gender='female', speaker_ageless=False)]
                reply = [row('Rain fell.', speaker='NARRATOR'),
                         row('Please open the gate. Take the blue cup.')]
                expected = [{**original[0], 'text': 'Rain fell.'},
                            {**original[0], 'text': 'Please open the gate.', 'speaker': 'ALICE',
                              'spoken': True, 'speaker_gender': 'female', 'speaker_ageless': False},
                            original[1]]
                expected[0].pop('pause_after')
                if nested_narrators:
                    original = [row('Rain fell.', speaker='NARRATOR', spoken=True,
                                    provenance={'flag': False}),
                                row('Wind rose.', speaker='NARRATOR', spoken=True,
                                    provenance={'flag': 0})]
                    reply = [row('Rain fell. Wind rose.', speaker='NARRATOR')]
                    expected = copy.deepcopy(original)
                source = Path(directory, 'fixture.json')
                source.write_text(json.dumps(original), encoding='utf-8')
                client, calls = client_for([(reply, 'stop')])
                config = {'llm_mode': 'remote', 'llm_remote': {'model_name': 'fixture'},
                          'generation': {'review_batch_size': 2, 'merge_narrators': True}}
                with ExitStack() as stack:
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(review.sys, 'argv',
                        ['review', '--input', str(source), '--context-window', str(context_window)]))
                    for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                        ('ensure_ideal_settings', (True, {}, 'fixture')),
                                        ('get_active_llm_config', config['llm_remote']),
                                        ('make_run_client', client), ('get_cached_or_benchmarked_concurrency', 1),
                                        ('get_current_status', {'loaded': False})):
                        stack.enter_context(patch.object(review, name, return_value=value))
                    stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                                    side_effect=AssertionError('No GPU calls')))
                    review.main()
                self.assertEqual(expected, json.loads(source.read_text(encoding='utf-8')))
                self.assertEqual(1, len(calls))
                self.assertFalse(Path(str(source) + '.review_checkpoint.json').exists())
                self.assertTrue(Path(str(source) + '.review_completed.json').exists())
