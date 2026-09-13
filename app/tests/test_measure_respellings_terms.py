"""--terms measures named terms the corpus threshold dropped, and refuses a
term it cannot read."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments.measure_respellings import rows_for_terms


class RowsForTermsTest(unittest.TestCase):
    def test_known_terms_keep_their_row_and_unknown_ones_are_built(self):
        cands = [{"term": "manga", "kana": "マンガ", "books": 3602, "series": 665, "verdict": "ja"}]
        rows = rows_for_terms(cands, ["Manga", "gaurururu", "subara"],
                              {"subara": ["Arc 1 - Volume 1", "Arc 1 - Volume 1_2"]}, "ja")
        self.assertEqual(["gaurururu", "manga", "subara"], [r["term"] for r in rows])
        self.assertEqual(3602, rows[1]["books"])           # the corpus row, untouched
        self.assertEqual("ガウルルル", rows[0]["kana"])
        self.assertEqual(0, rows[0]["books"])
        self.assertEqual(2, rows[2]["books"])
        self.assertEqual("--terms", rows[2]["source"])

    def test_a_term_that_does_not_romanise_is_refused(self):
        with self.assertRaises(SystemExit):
            rows_for_terms([], ["xqzv"], {}, "ja")


if __name__ == "__main__":
    unittest.main()
