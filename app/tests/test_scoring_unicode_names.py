"""`normalize` must not delete a writing system.

The pattern was `[^A-Z0-9 ]` - an ASCII allow-list applied as if it were a
punctuation stripper. Every CJK name became the empty string, `same_speaker`
returned False on its `if not b` guard, and the WP2021 arm scored 0 of 380
with `expected` and `predicted` BYTE-IDENTICAL on the rows it refused. The
real figure is 260 of 380, 68.4%.

Reporting that 0 as "attribution fails in Chinese" would have been the most
expensive false finding available: it is the exact shape of the answer goal
1.3 is looking for, and it was an artefact of a regex.

The English cases below are the regression guard. Re-scoring the stored RiQuA
and PDNC arms with the fix gives EXACTLY the same totals, so this changes what
can be scored and not how anything already scored was judged.
"""
import unittest

from experiments.scoring import normalize, same_speaker


class ScriptSurvivalTests(unittest.TestCase):
    def test_hanzi_survives(self):
        self.assertEqual(normalize("田晓霞"), "田晓霞")

    def test_kana_and_kanji_survive(self):
        self.assertEqual(normalize("ナツキ・スバル"), "ナツキスバル")
        self.assertEqual(normalize("先生"), "先生")

    def test_accented_latin_survives(self):
        # The old pattern turned JOSÉ into JOS, silently merging it with any
        # other JOS in the roster.
        self.assertEqual(normalize("JOSÉ"), "JOSÉ")
        self.assertEqual(normalize("Renée"), "RENÉE")

    def test_cyrillic_survives(self):
        self.assertEqual(normalize("Раскольников"), "РАСКОЛЬНИКОВ")

    def test_a_non_latin_name_matches_itself(self):
        # The whole failure in one assertion.
        self.assertTrue(same_speaker("田晓霞", "田晓霞"))

    def test_distinct_non_latin_names_stay_distinct(self):
        # The opposite error would be worse: a normalize that maps everything
        # to "" also makes every character equal to every other.
        self.assertFalse(same_speaker("田晓霞", "孙兰香"))
        self.assertFalse(same_speaker("ナツキ", "エミリア"))


class EnglishRegressionTests(unittest.TestCase):
    """These must not move. The fix is about what CAN be scored."""

    def test_punctuation_is_still_stripped(self):
        self.assertEqual(normalize("Mr. Knightley"), "MR KNIGHTLEY")
        self.assertEqual(normalize("MRS. BENNET"), "MRS BENNET")
        self.assertEqual(normalize("O'Brien"), "OBRIEN")

    def test_case_is_still_folded(self):
        self.assertEqual(normalize("emma"), normalize("EMMA"))

    def test_whitespace_is_still_collapsed(self):
        self.assertEqual(normalize("  MS.   SHORT   HAIR "), "MS SHORT HAIR")

    def test_the_honorific_cases_stay_distinct(self):
        # Named in the function's own docstring as what it exists for.
        self.assertNotEqual(normalize("MR. TALL"), normalize("MS. SHORT HAIR"))

    def test_underscore_is_removed(self):
        # \w keeps it; a roster label should not differ by one.
        self.assertEqual(normalize("A_B"), "AB")

    def test_empty_and_none_stay_empty(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_an_empty_prediction_is_never_correct(self):
        # The guard that turned the regex bug into silent zeros must remain.
        self.assertFalse(same_speaker("EMMA", ""))
        self.assertFalse(same_speaker("EMMA", None))
