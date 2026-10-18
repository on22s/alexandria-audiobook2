"""The #1290 blind re-decide instrument: what is hidden, and what the merge keeps."""
import unittest

from experiments.blind_review_redecide import get_blind_script, get_redecided_script


def row(speaker, text, spoken):
    return {'speaker': speaker, 'text': text, 'instruct': 'x', 'spoken': spoken}


ORIGINAL = [
    row('NARRATOR', 'The rain kept on.', False),
    row('ANNA', '"Shut the door."', True),
    row('BEN', '"It sticks."', True),
    row('NARRATOR', '"Mind the step."', True),  # spoken but mislabelled narration
    row('CLARA', 'Clara sighed.', False),       # a name on a narration row
]


class BlindScriptTests(unittest.TestCase):
    def test_hides_every_name_and_every_spoken_row_but_keeps_narration(self):
        blind = get_blind_script(ORIGINAL)
        self.assertEqual(['NARRATOR', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN'],
                         [r['speaker'] for r in blind])
        self.assertEqual([r['text'] for r in ORIGINAL], [r['text'] for r in blind])
        self.assertEqual('ANNA', ORIGINAL[1]['speaker'], 'the input must not be mutated')

    def test_no_original_name_survives_in_the_blind_script(self):
        names = {r['speaker'] for r in ORIGINAL} - {'NARRATOR'}
        self.assertFalse(names & {r['speaker'] for r in get_blind_script(ORIGINAL)})


class RedecideMergeTests(unittest.TestCase):
    def reviewed(self, *speakers):
        return [{**r, 'speaker': s} for r, s in zip(ORIGINAL, speakers)]

    def test_unknown_keeps_the_original_and_a_name_replaces_it(self):
        merged, counts = get_redecided_script(
            ORIGINAL, self.reviewed('NARRATOR', 'UNKNOWN', 'ANNA', 'DAN', ' unknown '))
        self.assertEqual(['NARRATOR', 'ANNA', 'ANNA', 'DAN', 'CLARA'],
                         [r['speaker'] for r in merged])
        self.assertEqual({'rows': 5, 'blinded': 4, 'kept_on_unknown': 2, 'relabelled': 2,
                          'confirmed': 0, 'narration_changed': 0}, counts)

    def test_empty_or_missing_labels_count_as_unknown(self):
        merged, counts = get_redecided_script(
            ORIGINAL, self.reviewed('NARRATOR', '', None, 'NARRATOR', 'CLARA'))
        self.assertEqual(['ANNA', 'BEN'], [r['speaker'] for r in merged[1:3]])
        self.assertEqual(2, counts['kept_on_unknown'])
        self.assertEqual(2, counts['confirmed'], 'NARRATOR on the spoken row and CLARA both match')

    def test_a_narration_row_the_model_relabels_is_counted_not_hidden(self):
        merged, counts = get_redecided_script(
            ORIGINAL, self.reviewed('ANNA', 'ANNA', 'BEN', 'NARRATOR', 'CLARA'))
        self.assertEqual('ANNA', merged[0]['speaker'])
        self.assertEqual(1, counts['narration_changed'])

    def test_refuses_a_changed_row_count_or_text(self):
        with self.assertRaises(ValueError):
            get_redecided_script(ORIGINAL, ORIGINAL[:-1])
        changed = self.reviewed('NARRATOR', 'ANNA', 'BEN', 'NARRATOR', 'CLARA')
        changed[2]['text'] = '"It sticks!"'
        with self.assertRaises(ValueError):
            get_redecided_script(ORIGINAL, changed)


if __name__ == '__main__':
    unittest.main()
