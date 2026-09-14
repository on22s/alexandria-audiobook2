import unittest

from text_diff import word_diff

SOURCE = ('"Where is he?" asked Holmes. "Gone," said I.\n\n'
          'Sherlock Holmes frowned and looked at the door for a long moment.')


class WordDiff(unittest.TestCase):
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
