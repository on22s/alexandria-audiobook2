import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments"))
from riqua_trainset import canonical_map, clean_name, document_rows, is_named  # noqa: E402

TXT = ('"Where is he?" asked Holmes. "Gone," said I. Sherlock Holmes frowned. '
       '"Then we wait," he said. He thought that the King would come. '
       '"I said \'never\' to him," said the King.')


def span(s, after=0):
    i = TXT.index(s, after)
    return (i, i + len(s))


Q1 = span('"Where is he?"')
Q2 = span('"Gone,"')
Q3 = span('"Then we wait,"')
IND = span("the King would come")          # indirect: no quote mark
Q4 = span('"I said \'never\' to him,"')
NEST = span("'never'")                     # nested inside Q4
E_HOLMES = span("Holmes")
E_I = span("I", Q2[1])
E_HE = span("he", Q3[1])
E_SHERLOCK = span("Sherlock Holmes")
E_KING = span("the King", Q4[1])


def ann():
    lines = [
        "T1\tQuotation %d %d\t%s" % (Q1[0], Q1[1], TXT[Q1[0]:Q1[1]]),
        "T2\tEntity %d %d\tHolmes" % E_HOLMES,
        "T3\tQuotation %d %d\t%s" % (Q2[0], Q2[1], TXT[Q2[0]:Q2[1]]),
        "T4\tEntity %d %d\tI" % E_I,
        "T5\tQuotation %d %d\t%s" % (Q3[0], Q3[1], TXT[Q3[0]:Q3[1]]),
        "T6\tEntity %d %d\the" % E_HE,
        "T7\tQuotation %d %d\t%s" % (IND[0], IND[1], TXT[IND[0]:IND[1]]),
        "T8\tEntity %d %d\tSherlock Holmes" % E_SHERLOCK,
        "T9\tQuotation %d %d\t%s" % (Q4[0], Q4[1], TXT[Q4[0]:Q4[1]]),
        "T10\tEntity %d %d\tthe King" % E_KING,
        "T11\tQuotation %d %d\t%s" % (NEST[0], NEST[1], TXT[NEST[0]:NEST[1]]),
        "R1\tSpeaker Arg1:T2 Arg2:T1",
        "R2\tSpeaker Arg1:T4 Arg2:T3",
        "R3\tSpeaker Arg1:T6 Arg2:T5",
        "R4\tSpeaker Arg1:T8 Arg2:T7",
        "R5\tSpeaker Arg1:T10 Arg2:T9",
        "R6\tSpeaker Arg1:T10 Arg2:T11",
        "R7\tAddressee Arg1:T8 Arg2:T9",
    ]
    return "\n".join(lines) + "\n"


class RiQuA(unittest.TestCase):
    def test_fixture_offsets_are_real(self):
        self.assertEqual(TXT[Q1[0]:Q1[1]], '"Where is he?"')
        self.assertEqual(TXT[Q4[0]:Q4[1]], '"I said \'never\' to him,"')
        self.assertEqual(TXT[NEST[0]:NEST[1]], "'never'")

    def test_named_vs_pronoun(self):
        self.assertTrue(is_named("the King"))
        self.assertFalse(is_named("he"))
        self.assertFalse(is_named("I"))
        self.assertFalse(is_named("a woman"))
        self.assertFalse(is_named("Mrs. Cratchit and the girls"))
        self.assertFalse(is_named("Charles, who was in bed"))
        self.assertFalse(is_named("His"))
        self.assertTrue(is_named("The Ghost."))
        self.assertEqual(canonical_map({"The Ghost", "The Ghost of Christmas Present"})["The Ghost"], "The Ghost of Christmas Present")

    def test_canonical_folds_into_the_one_longer_name(self):
        self.assertEqual(canonical_map({"Holmes", "Sherlock Holmes", "the King"})["Holmes"], "Sherlock Holmes")

    def test_rows_and_rejects(self):
        rows, rejected, roster = document_rows(TXT, ann(), "fixture", 400)
        self.assertEqual(roster, ["SHERLOCK HOLMES", "THE KING"])
        self.assertEqual([r["teacher"] for r in rows], ["SHERLOCK HOLMES", "THE KING"])
        self.assertEqual(rows[0]["line"], "Where is he?")          # quote marks stripped
        self.assertEqual(rows[1]["line"], "I said 'never' to him,")
        self.assertEqual(rejected, {"pronoun_or_unnamed_speaker": 2, "indirect": 1})
        # the nested quotation never became a unit
        self.assertFalse(any("never" == r["line"] for r in rows))
        # context is the narration between quotations, typed NARRATOR
        self.assertEqual(rows[0]["context"][2]["type"], "NARRATOR")
        self.assertTrue(rows[0]["context"][2]["text"].startswith("asked Holmes."))


if __name__ == "__main__":
    unittest.main()
