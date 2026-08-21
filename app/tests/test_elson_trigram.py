"""Hand-checked cases for the trigram rule, including the ones it must REJECT.

Rule 21: a rule that fires everywhere is a different, worse rule than one that
answers Elson's Quote-Said-Person category, and only a fixture set containing
things it must decline can tell the two apart. Every accept case below is a
real passage shape from Pride and Prejudice; every reject case is a shape that
looks like an attribution and is not one.
"""
import unittest

from experiments.elson_trigram import classify, resolve

ROSTER = ["MR. DARCY", "MR. BENNET", "MRS. BENNET", "ELIZABETH", "JANE",
          "COLONEL FITZWILLIAM", "MR. BINGLEY", "LYDIA"]
GROUPS = [{"ELIZABETH", "ELIZA", "LIZZY"}]


def after(text, line="Some quoted line."):
    return classify(line, "", text, ROSTER, GROUPS)


def before(text, line="Some quoted line."):
    return classify(line, text, "", ROSTER, GROUPS)


class AcceptTest(unittest.TestCase):
    def test_quote_said_person(self):
        """The exact row the model got wrong: expected MR. DARCY, said ELIZABETH."""
        self.assertEqual(
            ("quote_said_person", "MR. DARCY"),
            after('" said Mr. Darcy, looking at the eldest Miss Bennet.'))

    def test_quote_person_said(self):
        self.assertEqual(("quote_person_said", "JANE"),
                         after('" Jane replied, with a smile.'))

    def test_person_said_quote(self):
        self.assertEqual(("person_said_quote", "MRS. BENNET"),
                         before('Mrs. Bennet cried, "'))

    def test_a_newline_between_quote_and_attribution(self):
        """Gutenberg wraps lines; the gap must not be a single space."""
        self.assertEqual(("quote_said_person", "LYDIA"),
                         after('"\n\nsaid Lydia, laughing.'))

    def test_an_alias_resolves_to_the_roster_name(self):
        self.assertEqual(("quote_said_person", "ELIZABETH"),
                         after('" said Lizzy, colouring.'))

    def test_a_bare_surname_resolves_when_it_is_unambiguous(self):
        self.assertEqual(("quote_said_person", "MR. DARCY"),
                         after('" said Darcy.'))


class DeclineTest(unittest.TestCase):
    """Each of these must leave the model's answer alone."""

    def test_a_pronoun_is_the_anaphora_category_not_this_one(self):
        self.assertEqual(("no_pattern", None), after('" he said, and turned away.'))

    def test_a_nominal_is_not_resolved(self):
        """Elson built a nominal chunker; we did not, and must not pretend to."""
        self.assertEqual(("no_pattern", None), after('" said her father.'))

    def test_an_ambiguous_surname_is_declined(self):
        """BENNET names four cast members. Guessing one would be a coin flip."""
        self.assertEqual(("no_pattern", None), after('" said Bennet.'))

    def test_a_non_speech_verb_does_not_attribute(self):
        self.assertEqual(("no_pattern", None),
                         after('" Elizabeth walked towards the window.'))

    def test_a_place_name_is_not_a_speaker(self):
        self.assertEqual(("no_pattern", None), after('" said Netherfield.'))

    def test_a_name_with_no_verb_is_not_an_attribution(self):
        self.assertEqual(("no_pattern", None),
                         after('" Mr. Darcy was standing near them.'))

    def test_plain_narration_after_the_quote(self):
        self.assertEqual(("no_pattern", None),
                         after('" This was invitation enough.'))


class ResolveTest(unittest.TestCase):
    def test_pronouns_never_resolve(self):
        for word in ("he", "She", "they", "I", "it"):
            self.assertIsNone(resolve(word, ROSTER, GROUPS), word)

    def test_an_empty_surface_resolves_to_nothing(self):
        self.assertIsNone(resolve("", ROSTER, GROUPS))
        self.assertIsNone(resolve(None, ROSTER, GROUPS))


class AppliedSetTest(unittest.TestCase):
    """The name-first order is classified and reported, never applied.

    Measured on the 2,494 stored rows: <QUOTE> <PERSON> <VERB> scores 3 of 13,
    worse than the model it would override, and applying all four categories
    turns a +1.24 result into +0.96 with 8 rows broken. Keeping it out is the
    measurement's verdict, so a change that quietly lets it back in must fail.
    """

    def test_the_harmful_order_is_not_applied_by_default(self):
        from experiments.elson_trigram import APPLY_BY_DEFAULT
        self.assertNotIn("quote_person_said", APPLY_BY_DEFAULT)
        self.assertIn("quote_said_person", APPLY_BY_DEFAULT)

    def test_it_is_still_classified_so_the_number_stays_visible(self):
        """Not applying is not the same as not looking."""
        self.assertEqual(("quote_person_said", "JANE"),
                         after('" Jane replied, with a smile.'))

    def test_subsets_reports_every_cumulative_choice(self):
        from experiments.elson_trigram import subsets
        rows = [
            {"fired": True, "category": "quote_said_person",
             "rule_correct": True, "model_correct": False},
            {"fired": True, "category": "quote_person_said",
             "rule_correct": False, "model_correct": True},
            {"fired": False, "category": "no_pattern",
             "rule_correct": False, "model_correct": True},
        ]
        out = subsets(rows)
        self.assertEqual(1, out["quote_said_person"]["fixed"])
        self.assertEqual(0, out["quote_said_person"]["broke"])
        widest = "quote_said_person+said_person_quote+person_said_quote+quote_person_said"
        self.assertEqual(1, out[widest]["broke"],
                         "the widest subset must show the row it breaks")
        self.assertLess(out[widest]["delta_points"],
                        out["quote_said_person"]["delta_points"])
