"""Hand-checked cases for the ported instruct rules (Rule 21) - including
the two findings the port gets WRONG on this project's English instructs,
pinned as known limitations so a fix flips them on purpose."""
import unittest

from experiments.instruct_lexicon import audit_instruct, count_clauses


def codes(text):
    return {f["code"] for f in audit_instruct(text)}


class TrustworthyFindings(unittest.TestCase):
    def test_timbre_words_are_flagged_and_delivery_words_are_not(self):
        self.assertIn("timbre", codes("Author name announcement; clear and resonant voice."))
        self.assertIn("timbre", codes("Gravelly narration; descriptive language delivered with heavy effort."))
        self.assertNotIn("timbre", codes("Cold fury, barely contained, voice tight"))
        self.assertNotIn("timbre", codes("Clear, formal announcement tone; measured pace."))

    def test_clause_and_length_limits(self):
        self.assertEqual(3, count_clauses("Cold fury, barely contained, voice tight"))
        self.assertIn("over_specified", codes("Sad, slow, quiet, trembling, then a pause."))
        self.assertIn("long", codes("Sharp, sudden gasp of relief or surprise; high pitch and immediate burst of sound."))
        self.assertEqual(set(), codes("Strong, definitive delivery; slightly elevated pitch for emphasis."))


class KnownLimitationsOnEnglish(unittest.TestCase):
    """The fork's lexicons were built for Chinese-first use; on our English
    instructs two findings fire on conformant lines. instruct_audit's
    numbers for these codes are NOT to be read as defects."""

    def test_no_delivery_misses_common_english_delivery_words(self):
        # "smooth delivery", "quick, precise reading" are delivery language
        self.assertIn("no_delivery", codes("Informative, smooth delivery; slightly softer than the author's name."))
        self.assertIn("no_delivery", codes("Quick, precise reading of the copyright line; professional quality."))

    def test_punctuation_fires_on_an_apostrophe(self):
        self.assertIn("punctuation", codes("Dramatic summation; slowing down on 'checkmate' for impact."))
        self.assertIn("punctuation", codes("Softer than the author's name."))
