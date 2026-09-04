"""The stored summary must follow from the rows beside it.

WHY THIS EXISTS. ExperimentRecord.validate() carried two checks for exactly
this - `summary n=... but N rows` and `summary correct=... but rows give N`.
They could not fail. summary() DERIVES n and correct by counting self.rows,
and validate() then compared that derived value back against the same rows:
a value against its own source. A trace over all 2,696 tests on 2026-09-04
found neither line ever executed - not because nothing tried, but because no
input could reach them.

The concern was right. Every figure quoted from this repository is read out of
a stored `summary` block, and nothing checked it against the rows in the same
file. Inside a live record they agree by construction; in a FILE they are two
records that a hand edit, a merge, a truncation or an older writer can put out
of step.
"""
import unittest

from experiments.manifest import validate_stored_summary


def _doc(summary, rows):
    return {"summary": summary, "rows": rows}


def _rows(arm, n, correct):
    return [{"arm": arm, "id": i, "correct": i < correct} for i in range(n)]


class StoredSummary(unittest.TestCase):
    def test_a_faithful_summary_passes(self):
        doc = _doc({"base": {"n": 10, "correct": 7}}, _rows("base", 10, 7))
        self.assertEqual([], validate_stored_summary(doc))

    def test_an_inflated_correct_count_is_caught(self):
        """The failure the old guard was written for and could not see."""
        doc = _doc({"base": {"n": 10, "correct": 9}}, _rows("base", 10, 7))
        problems = validate_stored_summary(doc)
        self.assertTrue(any("correct=9" in p and "rows give 7" in p
                            for p in problems), problems)

    def test_a_truncated_rows_list_is_caught(self):
        """Half a file transferred, or a writer that stopped early."""
        doc = _doc({"base": {"n": 100, "correct": 70}}, _rows("base", 40, 30))
        problems = validate_stored_summary(doc)
        self.assertTrue(any("n=100" in p and "40 rows" in p
                            for p in problems), problems)

    def test_an_arm_in_the_summary_with_no_rows_is_caught(self):
        doc = _doc({"base": {"n": 10, "correct": 7}, "tuned": {"n": 10, "correct": 8}},
                   _rows("base", 10, 7))
        self.assertTrue(any("tuned" in p and "no rows" in p
                            for p in validate_stored_summary(doc)))

    def test_rows_for_an_arm_the_summary_omits_are_caught(self):
        doc = _doc({"base": {"n": 10, "correct": 7}},
                   _rows("base", 10, 7) + _rows("tuned", 10, 8))
        self.assertTrue(any("tuned" in p and "omits" in p
                            for p in validate_stored_summary(doc)))

    def test_the_wide_schema_is_not_judged(self):
        """aishell3_score, ljspeech_score and friends put each arm in its own
        COLUMN - no row carries an `arm` key. A first version of this check
        counted no arms there, called every summary bucket orphaned, and
        reported 105 of 397 artifacts inconsistent. All 105 were this
        mismatch."""
        wide = {"summary": {"lora": {"n": 150}, "clone": {"n": 150}},
                "rows": [{"id": i, "lora": 0.5, "clone": 0.4} for i in range(150)]}
        self.assertEqual([], validate_stored_summary(wide))

    def test_a_document_without_the_pair_is_not_judged(self):
        """Not every artifact carries both; absence is not disagreement."""
        self.assertEqual([], validate_stored_summary({"summary": {"base": {}}}))
        self.assertEqual([], validate_stored_summary({"rows": []}))


if __name__ == "__main__":
    unittest.main()
