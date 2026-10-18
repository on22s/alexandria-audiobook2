import unittest
from unittest.mock import patch

from text_diff import word_diff

SOURCE = ('"Where is he?" asked Holmes. "Gone," said I.\n\n'
          'Sherlock Holmes frowned and looked at the door for a long moment.')


class WordDiff(unittest.TestCase):
    def test_unspaced_dialogue_boundaries_do_not_invent_word_changes(self):
        source = 'He said, "Hello"—then left.'
        entries = [{"text": "He said, "}, {"text": "Hello"},
                   {"text": "—then left."}]
        result = word_diff(source, entries)
        self.assertEqual([], result["hunks"])
        self.assertEqual({"source_words": 5, "script_words": 5,
                          "deleted": 0, "inserted": 0, "replaced": 0},
                         result["totals"])

    def test_native_quotes_and_typographic_dashes_keep_literal_word_boundaries(self):
        cases = [('"', '"'), ('“', '”'), ('「', '」'), ('『', '』')]
        for opening, closing in cases:
            for dash in ('—', '–'):
                with self.subTest(opening=opening, dash=dash):
                    source = f'Before{opening}Hello{closing}{dash}after.'
                    entries = [{"text": "Before"}, {"text": "Hello"},
                               {"text": f"{dash}after."}]
                    result = word_diff(source, entries)
                    self.assertEqual([], result["hunks"])
                    self.assertEqual(3, result["totals"]["source_words"])
                    self.assertEqual(3, result["totals"]["script_words"])

    def test_en_dash_compound_matches_hyphen_compound(self):
        cases = [
            ('well–being matters', [{'text': 'well-being matters'}]),
            ('well-being matters', [{'text': 'well–being matters'}]),
            ('1990–1995 was quiet', [{'text': '1990-1995 was quiet'}]),
            ('1990-1995 was quiet', [{'text': '1990–1995 was quiet'}]),
        ]
        for source, entries in cases:
            with self.subTest(source=source):
                result = word_diff(source, entries)
                self.assertEqual([], result["hunks"])
                self.assertEqual(0, result["totals"]["deleted"])
                self.assertEqual(0, result["totals"]["inserted"])
                self.assertEqual(0, result["totals"]["replaced"])

    def test_em_dash_between_words_is_still_a_boundary(self):
        result = word_diff('Hello—after',
                           [{'text': 'Hello'}, {'text': 'after'}])
        self.assertEqual([], result["hunks"])
        self.assertEqual(2, result["totals"]["source_words"])
        self.assertEqual(2, result["totals"]["script_words"])

    def test_spaced_en_dash_is_still_a_boundary(self):
        result = word_diff('a – b', [{'text': 'a'}, {'text': 'b'}])
        self.assertEqual([], result["hunks"])
        self.assertEqual(2, result["totals"]["source_words"])
        self.assertEqual(2, result["totals"]["script_words"])

    def test_closing_quote_alone_keeps_word_boundary(self):
        entries = [{'text': 'Before'}, {'text': 'Hello'},
                   {'text': 'after'}]
        result = word_diff('Before“Hello”after', entries)
        self.assertEqual([], result["hunks"])
        self.assertEqual(3, result["totals"]["source_words"])
        self.assertEqual(3, result["totals"]["script_words"])

    def test_real_change_next_to_en_dash_compound_has_hunk(self):
        result = word_diff('well–being matters',
                           [{'text': 'well-being changes'}])
        self.assertTrue(result["hunks"])
        self.assertGreater(result["totals"]["replaced"], 0)

    def test_comparison_tokens_have_independent_known_answers(self):
        from text_diff import get_comparison_words
        cases = [
            ('He said, "Hello"—then left.', ['he', 'said', 'hello', 'then', 'left']),
            ('前「こんにちは」と彼は言った。', ['前', 'こんにちは', 'と彼は言った']),
            ('«ordinary» *formatting*, stays.', ['ordinary', 'formatting', 'stays']),
            ("Don't change well-being or Mary's book.",
             ['dont', 'change', 'wellbeing', 'or', 'marys', 'book']),
            ('Don’t change well-being or Mary’s book.',
             ['dont', 'change', 'wellbeing', 'or', 'marys', 'book']),
            ('', []),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, get_comparison_words(source))

    def test_real_word_changes_and_harmful_splits_still_have_hunks(self):
        cases = [
            ('nowhere', 'now here'),
            ('therapist', 'the rapist'),
            ('alpha beta', 'alpha extra beta'),
            ('alpha beta', 'alpha'),
            ('alpha beta', 'alpha gamma'),
            ('alpha beta', 'beta alpha'),
            ("don't leave", 'don t leave'),
            ('don’t leave', 'do not leave'),
            ('well-being matters', 'well being matters'),
            ('He said, "Hello"—then left.', 'He said Goodbye then left.'),
        ]
        for source, changed in cases:
            with self.subTest(source=source, changed=changed):
                result = word_diff(source, [{"text": changed}])
                self.assertTrue(result["hunks"])
                self.assertGreater(sum(result["totals"][kind]
                                       for kind in ("deleted", "inserted", "replaced")), 0)
        self.assertEqual([], word_diff('well-being matters',
                                       [{"text": 'wellbeing matters'}])["hunks"])

    def test_boundary_adjacent_change_keeps_owner_chunk_and_word_offsets(self):
        first = 'Start."Hello"—then wait.'
        second = 'Next."Goodbye"–then gone.'
        entries = [{"text": "Start."}, {"text": "Hello"},
                   {"text": "—then wait."}, {"text": "Next."},
                   {"text": "Farewell"}, {"text": "–then gone."}]
        with patch('text_diff.split_into_chunk_records',
                   return_value=[{"text": first}, {"text": second}]):
            result = word_diff(first + '\n\n' + second, entries)
        self.assertEqual(2, result["chunks_total"])
        self.assertEqual(8, result["totals"]["source_words"])
        self.assertEqual(8, result["totals"]["script_words"])
        self.assertEqual(1, len(result["hunks"]))
        hunk = result["hunks"][0]
        self.assertEqual(('replace', 5, 2, 4, 'goodbye', 'farewell'),
                         tuple(hunk[key] for key in
                               ('kind', 'source_pos', 'chunk', 'entry_index',
                                'source_words', 'script_words')))
        self.assertEqual('then wait next', hunk['source_before'])
        self.assertEqual('then gone', hunk['source_after'])

    def test_last_entry_deletion_after_quote_boundary_keeps_previous_owner(self):
        result = word_diff('He said,"Hello"—then left.',
                           [{"text": 'He said,'}, {"text": 'Hello'}])
        self.assertEqual(1, len(result['hunks']))
        hunk = result['hunks'][0]
        self.assertEqual(('delete', 3, 1, 'then left', ''),
                         tuple(hunk[key] for key in
                               ('kind', 'source_pos', 'entry_index', 'source_words', 'script_words')))
        self.assertEqual(2, result['totals']['deleted'])

    def test_identical_text_has_no_hunks_whatever_the_punctuation(self):
        entries = [{"text": "Where is he?"}, {"text": "asked Holmes."}, {"text": "Gone,"},
                   {"text": "said I. *Sherlock Holmes* frowned and looked at the door for a long moment."}]
        out = word_diff(SOURCE, entries)
        self.assertEqual([], out["hunks"])
        self.assertEqual(out["totals"]["source_words"], out["totals"]["script_words"])

    def test_dropped_and_expanded_text_are_located(self):
        entries = [{"text": "Where is he?"}, {"text": "asked Holmes."},
                   # "Gone, said I." dropped; "very long" expanded
                   {"text": "Sherlock Holmes frowned and looked at the door for a very long moment."}]
        out = word_diff(SOURCE, entries)
        kinds = [(h["kind"], h["source_words"], h["script_words"]) for h in out["hunks"]]
        self.assertIn(("delete", "gone said i", ""), kinds)
        self.assertIn(("insert", "", "very"), kinds)
        dropped = next(h for h in out["hunks"] if h["kind"] == "delete")
        self.assertEqual("he asked holmes", dropped["source_before"])
        self.assertEqual(1, dropped["chunk"])
        self.assertEqual(2, next(h for h in out["hunks"] if h["kind"] == "insert")["entry_index"])
        self.assertEqual(3, out["totals"]["deleted"])
        self.assertEqual(1, out["totals"]["inserted"])


if __name__ == "__main__":
    unittest.main()
