import copy
import unittest
from unittest.mock import patch

from dialogue_continuity import get_dialogue_continuity_plan


class DialogueContinuityTests(unittest.TestCase):
    def rows(self):
        return [{'uid': 'a', 'speaker': 'Anna', 'text': 'I will go,'},
                {'uid': 'n', 'speaker': 'NARRATOR', 'text': 'Anna said,'},
                {'uid': 'b', 'speaker': 'Anna', 'text': 'when the rain stops.'}]

    def plan(self, rows):
        return get_dialogue_continuity_plan(rows, narrator_speakers=['NARRATOR'])

    def test_attached_tag_and_resumed_sentence_keep_exact_words_speakers_and_inputs(self):
        rows = self.rows(); before = copy.deepcopy(rows); plan = self.plan(rows)
        self.assertEqual(['speech_before_tag', 'parenthetical_tag', 'resumed_speech'],
                         [r['role'] for r in plan['rows']])
        self.assertEqual([120, 120, None], [r['deterministic']['pause_after'] for r in plan['rows']])
        for row, result in zip(rows, plan['rows']):
            self.assertEqual(row['text'], result['baseline']['text'])
            self.assertEqual(row['text'], result['deterministic']['text'])
            self.assertEqual(row['speaker'], result['speaker'])
        plan['rows'][0]['deterministic']['text'] = 'Output-only change'
        self.assertEqual(before, rows)
        self.assertEqual(['hidden_context'], plan['pending_arms'])

    def test_independent_indirect_ambiguous_and_other_speaker_tags_do_not_attach(self):
        for text in ('Anna walked away, then said nothing.', 'Anna said nothing.',
                     'Ben said,', 'She said,', 'A voice replied,'):
            with self.subTest(text=text):
                rows = self.rows(); rows[1]['text'] = text
                self.assertIsNone(self.plan(rows)['rows'][0]['role'])
                self.assertNotEqual('parenthetical_tag', self.plan(rows)['rows'][1]['role'])
        rows = self.rows(); rows[1]['speaker'] = 'NARRATOR_CHARACTER'
        self.assertIsNone(self.plan(rows)['rows'][0]['role'])

    def test_resumed_speech_can_also_lead_into_another_attribution(self):
        rows = self.rows()
        rows[2]['text'] = 'when the rain stops,'
        rows.extend([{'uid': 'n2', 'speaker': 'NARRATOR', 'text': 'Anna added,'},
                     {'uid': 'c', 'speaker': 'Anna', 'text': 'and the road clears.'}])
        result = self.plan(rows)['rows'][2]
        self.assertEqual('resumed_speech_before_tag', result['role'])
        self.assertIn('Resume the same sentence', result['suggested_instruct'])
        self.assertIn('without a full stop', result['suggested_instruct'])
        self.assertEqual(120, result['deterministic']['pause_after'])
        self.assertEqual(rows[2]['text'], result['deterministic']['text'])
        rows[1]['text'] = 'Anna said.'
        self.assertEqual('speech_before_tag', self.plan(rows)['rows'][2]['role'])

    def test_resumed_sentence_preserves_interruption_into_another_speaker(self):
        rows = self.rows(); rows[2]['text'] = 'that I—'
        rows.append({'uid': 'other', 'speaker': 'Ben', 'text': 'Listen.'})
        result = self.plan(rows)['rows'][2]
        self.assertEqual('resumed_interrupted_speech', result['role'])
        self.assertIn('Resume', result['suggested_instruct'])
        self.assertIn('interruption', result['suggested_instruct'])
        self.assertEqual(0, result['deterministic']['pause_after'])
        self.assertEqual(rows[2]['text'], result['deterministic']['text'])

    def test_explicit_instruction_empty_zero_and_cleared_pause_win(self):
        for instruction, pause in (('Firm and complete.', 900), ('', 0), ('', None)):
            with self.subTest(instruction=instruction, pause=pause):
                rows = self.rows(); rows[0].update(instruct=instruction, pause_after=pause)
                row = self.plan(rows)['rows'][0]
                self.assertEqual(instruction, row['deterministic']['instruct'])
                self.assertEqual(pause, row['deterministic']['pause_after'])
                self.assertEqual('explicit', row['instruction_source'])
                self.assertEqual('explicit', row['pause_source'])

    def test_ellipsis_terminal_cadence_interruption_and_independent_narration(self):
        for text, word in (('Could we…', 'ellipsis'), ('Will you?', 'question'), ('Leave!', 'exclamation')):
            rows = self.rows(); rows[0]['text'] = text
            self.assertIn(word, self.plan(rows)['rows'][0]['suggested_instruct'])
        rows = [{'uid': 'x', 'speaker': 'Anna', 'text': 'But I—'},
                {'uid': 'y', 'speaker': 'Ben', 'text': 'Listen.'}]
        self.assertEqual(0, self.plan(rows)['rows'][0]['deterministic']['pause_after'])
        rows[0].update(speaker='NARRATOR', text='The door closed.')
        self.assertEqual('independent_narration', self.plan(rows)['rows'][0]['role'])

    def test_generated_notes_can_receive_hints_but_manual_notes_remain_authoritative(self):
        for note in ('Speak softly.', ''):
            rows = self.rows(); rows[0].update(instruct=note, instruction_origin='generated', pause_after=0)
            before = copy.deepcopy(rows)
            result = self.plan(rows)['rows'][0]
            expected = '\n'.join(part for part in (note, result['suggested_instruct']) if part)
            self.assertEqual(note, result['baseline']['instruct'])
            self.assertEqual(expected, result['deterministic']['instruct'])
            self.assertEqual('generated_with_suggestion', result['instruction_source'])
            self.assertEqual(0, result['deterministic']['pause_after'])
            self.assertEqual(before, rows)
            generated_digest = result['delivery_digest']
            for origin in ('manual', None):
                if origin is None:
                    rows[0].pop('instruction_origin')
                else:
                    rows[0]['instruction_origin'] = origin
                manual = self.plan(rows)['rows'][0]
                self.assertEqual(note, manual['deterministic']['instruct'])
                self.assertNotEqual(generated_digest, manual['delivery_digest'])
        rows = self.rows(); rows[0].update(instruct='Speak softly.', instruction_origin='generated')
        rows[1]['text'] = 'Anna walked away.'
        result = self.plan(rows)['rows'][0]
        self.assertEqual(result['baseline'], result['deterministic'])
        self.assertEqual('generated', result['instruction_source'])

    def test_ambiguous_instruction_origins_are_rejected(self):
        for origin in ('', 'Generated', 'unknown', None, True, [], {}):
            rows = self.rows(); rows[0].update(instruct='Note.', instruction_origin=origin)
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, 'origin'):
                self.plan(rows)
        rows = self.rows(); rows[0]['instruction_origin'] = 'generated'
        with self.assertRaisesRegex(ValueError, 'origin'):
            self.plan(rows)

    def test_material_delivery_identity_ignores_metadata_and_post_render_pause(self):
        rows = self.rows(); initial = self.plan(rows)
        rows[0].update(status='done', duration=1.5, custom={'unrelated': object()}, pause_after=1000)
        changed = self.plan(rows)
        self.assertEqual(initial['rows'][0]['delivery_digest'], changed['rows'][0]['delivery_digest'])
        self.assertNotEqual(initial['source_digest'], changed['source_digest'])
        rows[1]['text'] = 'Anna walked away.'
        self.assertNotEqual(initial['rows'][0]['delivery_digest'], self.plan(rows)['rows'][0]['delivery_digest'])
        rows[0]['instruct'] = 'Manual note.'; explicit = self.plan(rows)
        rows[1]['text'] = 'Anna said,'
        self.assertEqual(explicit['rows'][0]['delivery_digest'], self.plan(rows)['rows'][0]['delivery_digest'])

    def test_bounds_invalid_shapes_and_duplicate_identity_refuse_before_a_plan(self):
        bad = [[], [{}], self.rows() * 2, [{'uid': 'a', 'speaker': 'Anna', 'text': '\ud800'}]]
        for rows in bad:
            with self.subTest(rows=repr(rows)), self.assertRaises(ValueError):
                self.plan(rows)
        for pause in (True, -1, 3600001, '10'):
            rows = self.rows(); rows[0]['pause_after'] = pause
            with self.subTest(pause=pause), self.assertRaises(ValueError):
                self.plan(rows)
        with patch('dialogue_continuity.MAX_CONTINUITY_BYTES', 1), self.assertRaisesRegex(ValueError, '2 MiB'):
            self.plan(self.rows())
