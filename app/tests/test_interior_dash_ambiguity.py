"""Unknown interior turns must not become measured non-spoken narration."""
import copy
import unittest

from dialogue_spans import apply_dialogue_map, mark_entries, spoken_spans


CONTROLS = '\n\n'.join(f'— Проверка {i}, — сказал Иван.' for i in range(5))


class InteriorDashAmbiguityTests(unittest.TestCase):
    def test_exact_issue_turn_is_located_unknown_and_visibly_reported(self):
        source = CONTROLS + '\n\nОни вошли. — Нет, — сказала Анна.'
        entries = [{'speaker': 'Анна', 'text': 'Нет, — сказала Анна.',
                    'custom_metadata': {'keep': True}}]
        before = copy.deepcopy(entries)
        with self.assertLogs('dialogue_spans', 'WARNING') as logs:
            result = apply_dialogue_map(entries, source)
        row = result['entries'][0]
        self.assertEqual('dash_lines', result['convention'])
        self.assertEqual(1, result['located'])
        self.assertEqual(0, result['spoken'])
        self.assertEqual(1, result['ambiguous'])
        self.assertEqual('interior_dash_turn', row['dialogue_ambiguity'])
        self.assertNotIn('spoken', row)
        self.assertNotIn('source_speaker', row)
        self.assertEqual(entries[0]['text'], source[slice(*row['source_span'])])
        self.assertEqual('Анна', row['speaker'])
        self.assertEqual({'keep': True}, row['custom_metadata'])
        self.assertEqual([], result['speaker_changes'])
        self.assertIn('spoken status is unestablished', logs.output[0])
        self.assertEqual(before, entries)
        self.assertEqual(5, len(spoken_spans(source, 'dash_lines')))

    def test_real_paragraph_boundary_still_establishes_speech_without_ambiguity(self):
        source = CONTROLS + '\n\nОни вошли.\n\n— Нет, — сказала Анна.'
        entries = [{'speaker': 'Анна', 'text': 'Нет, — сказала Анна.'}]
        result = apply_dialogue_map(entries, source)
        self.assertTrue(result['entries'][0]['spoken'])
        self.assertNotIn('dialogue_ambiguity', result['entries'][0])
        self.assertEqual(1, result['spoken'])
        self.assertEqual(0, result['ambiguous'])
        self.assertEqual(6, len(spoken_spans(source, 'dash_lines')))

    def test_narration_apposition_math_and_unpaired_dashes_remain_nonspoken(self):
        for text in ('The road — long, narrow and quiet — stretched away.',
                     'The thought returned. — perhaps — and then it faded.',
                     'The figure was 2. — 3, — 4.',
                     'They arrived. — A passing thought.',
                     'They arrived. A passing thought, — then they left.'):
            with self.subTest(text=text):
                result = apply_dialogue_map([{'speaker': 'NARRATOR', 'text': text}],
                                            CONTROLS + '\n\n' + text)
                self.assertFalse(result['entries'][0]['spoken'])
                self.assertEqual(0, result['ambiguous'])
                self.assertNotIn('dialogue_ambiguity', result['entries'][0])

    def test_raw_unicode_offsets_and_tag_only_narration_are_preserved(self):
        for prefix, turn, tag in (('🙂 Они вошли.', 'Нет,', 'сказала Анна.'),
                                  ('They arrived!', 'No?', 'Anna asked.'),
                                  ('Llegaron.', 'No!', 'dijo Ana.')):
            with self.subTest(prefix=prefix):
                source = CONTROLS + f'\n\n{prefix}\t—\t{turn}\t—\t{tag}'
                entries = [{'speaker': 'NARRATOR', 'text': prefix},
                           {'speaker': 'Anna', 'text': turn},
                           {'speaker': 'NARRATOR', 'text': tag}]
                before = copy.deepcopy(entries)
                result = apply_dialogue_map(entries, source)
                self.assertEqual(3, result['located'])
                self.assertEqual(1, result['ambiguous'])
                self.assertEqual(0, result['spoken'])
                self.assertFalse(result['entries'][0]['spoken'])
                self.assertNotIn('spoken', result['entries'][1])
                self.assertFalse(result['entries'][2]['spoken'])
                for row in result['entries']:
                    self.assertEqual(row['text'], source[slice(*row['source_span'])])
                self.assertEqual(before, entries)

    def test_repeated_out_of_order_mapping_and_remapping_do_not_retain_stale_flags(self):
        interior = 'They arrived. — No, — Anna said.'
        source = CONTROLS + '\n\n' + interior + '\n\nOrdinary narration.\n\n' + interior
        entries = [{'speaker': 'NARRATOR', 'text': 'Ordinary narration.'},
                   {'speaker': 'Anna', 'text': 'No,'},
                   {'speaker': 'Anna', 'text': 'No,'}]
        result = apply_dialogue_map(entries, source)
        self.assertEqual(3, result['located'])
        self.assertEqual(2, result['ambiguous'])
        rows = result['entries']
        self.assertNotEqual(rows[1]['source_span'], rows[2]['source_span'])
        for row in rows[1:]:
            self.assertNotIn('spoken', row)
            self.assertEqual('No,', source[slice(*row['source_span'])])
        remapped = mark_entries(rows[1:], '“No,”\n\n“No,”', 'paired_quotes')
        self.assertTrue(all(row['spoken'] for row in remapped))
        self.assertTrue(all('dialogue_ambiguity' not in row for row in remapped))

    def test_other_conventions_and_insufficient_evidence_keep_existing_contract(self):
        source = 'They arrived. — No, — Anna said.'
        entry = {'speaker': 'Anna', 'text': 'No,'}
        result = apply_dialogue_map([entry], source)
        self.assertIsNone(result['convention'])
        self.assertEqual([entry], result['entries'])
        self.assertEqual(0, result['ambiguous'])
        mapped = mark_entries([entry], source, 'paired_quotes')
        self.assertFalse(mapped[0]['spoken'])
        self.assertNotIn('dialogue_ambiguity', mapped[0])


if __name__ == '__main__':
    unittest.main()
