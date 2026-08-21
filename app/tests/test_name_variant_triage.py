"""Which of 5.2's "inconsistent names" the lexicon could actually help.

Four variant groups back that goal's 5.07% baseline. Two are not name variants:
`Knights` is a common noun from a chapter title, and `Ferri's` is the character
Ferris calling himself Ferri and contracting "is". The other two - `Su-ba-ru`
and `R-e-m` - are the author drawing a name out, six occurrences in total, and
an engine voicing separate syllables may be doing what the page asks.

Three bugs were found writing this, all of the same kind: a confident answer
built on a silent absence.

- An EMPTY ROSTER classified every group as not-a-character. The cast files are
  untracked so a worktree has none, and the result looked entirely plausible.
  It is now fatal.
- The stretch pattern required a single leading character, so it matched
  `R-e-m` and missed `Su-ba-ru`, splitting a matched pair of identical cases.
- The verdict was called `not_a_character` when the check can only see whether
  a name is in the SUPPLIED cast list. Ferris is a real character absent from
  voice_config. Renamed to say what it knows.
"""
import unittest

from experiments.name_variant_triage import STRETCHED, classify, load_roster

ROSTER = {"subaru", "rem", "emilia"}


class StretchPatternTest(unittest.TestCase):
    def test_both_real_stretches_match(self):
        """The bug: `\\w` matched R-e-m and missed Su-ba-ru."""
        self.assertTrue(STRETCHED.match("Su-ba-ru"))
        self.assertTrue(STRETCHED.match("R-e-m"))

    def test_a_plain_name_does_not_match(self):
        self.assertFalse(STRETCHED.match("Subaru"))
        self.assertFalse(STRETCHED.match("Rem"))

    def test_an_apostrophe_form_is_not_a_stretch(self):
        self.assertFalse(STRETCHED.match("Ferri’s"))


class ClassifyTest(unittest.TestCase):
    def test_a_drawn_out_name_is_a_deliberate_stretch(self):
        verdict, why = classify({"Subaru": 2793, "Su-ba-ru": 3}, ROSTER)
        self.assertEqual("deliberate_stretch", verdict)
        self.assertIn("may be correct", why)

    def test_the_stretch_must_be_the_minority_spelling(self):
        """A genuinely hyphenated name is not a stretch of anything."""
        verdict, _ = classify({"Jean-Luc": 900, "Jeanluc": 2},
                              {"jean-luc", "jeanluc"})
        self.assertNotEqual("deliberate_stretch", verdict)

    def test_an_apostrophe_form_is_reported_as_such(self):
        verdict, why = classify({"Rem": 890, "Rem’s": 40}, ROSTER)
        self.assertEqual("contraction_or_possessive", verdict)
        self.assertIn("apostrophe", why)

    def test_a_name_the_cast_list_does_not_know_is_flagged_honestly(self):
        """It cannot tell a common noun from an uncast character."""
        verdict, why = classify({"knights": 86, "Knights": 35}, ROSTER)
        self.assertEqual("not_in_cast_list", verdict)
        self.assertIn("supplied cast list", why)

    def test_an_unexplained_difference_stays_genuine(self):
        verdict, _ = classify({"Emilia": 500, "Emillia": 4}, ROSTER)
        self.assertEqual("genuine_variant", verdict)


class RosterTest(unittest.TestCase):
    def test_a_missing_file_contributes_nothing_rather_than_raising(self):
        self.assertEqual(set(), load_roster(["/does/not/exist.json"]))

    def test_keys_and_alias_values_both_count(self):
        import json, os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "aliases.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"BEATRICE": ["Betty"], "SUBARU": []}, handle)
        self.assertEqual({"beatrice", "betty", "subaru"}, load_roster([path]))

    def test_an_empty_roster_makes_everything_look_uncast(self):
        """Why main() refuses: this is the wrong answer that looks right."""
        verdict, _ = classify({"Subaru": 2793, "Su-ba-ru": 3}, set())
        self.assertEqual("not_in_cast_list", verdict)
