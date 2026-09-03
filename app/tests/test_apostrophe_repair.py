"""A repair that fires on healthy text is worse than the damage.

Ground truth exists: strip apostrophes from a healthy novel, repair, compare.
Measured over 18 PDNC novels with >=200 apostrophes - 26,681 of them - this
recovers 83.7% and falsely inserts 66. Every test below pins one property that
measurement depends on.
"""
import unittest

from apostrophe_repair import looks_damaged, restore_stripped_apostrophes
from generate_script import get_preprocessed_source

DAMAGED = 'I don t know. It was Miller s house. I m sure you re right.'
HEALTHY = "I don't know. It was Miller's house."


class Detection(unittest.TestCase):
    def test_damaged_signature_is_recognised(self):
        self.assertTrue(looks_damaged(DAMAGED))

    def test_a_lone_apostrophe_does_not_mask_heavy_damage(self):
        """HowardsEnd carries ONE apostrophe against 763 broken contractions.

        An absolute "any apostrophe means healthy" gate excluded the single
        worst-damaged novel in the corpus. The gate is proportional for that
        reason; this pins the shape of that book.
        """
        text = "He said 'so' once. " + "I don t know. " * 40
        self.assertTrue(looks_damaged(text))

    def test_healthy_prose_is_never_flagged(self):
        self.assertFalse(looks_damaged(HEALTHY))
        self.assertFalse(looks_damaged("it's fine, he doesn't mind, she can't stay"))

    def test_a_short_damaged_excerpt_is_still_repaired(self):
        """A pasted excerpt needs the repair as much as a whole novel."""
        self.assertTrue(looks_damaged("I don t know. She can t stay."))

    def test_one_stray_break_in_healthy_prose_is_ignored(self):
        """The ratio, not a count, is what protects a healthy book."""
        healthy = "it's fine, " * 200 + "don t"
        self.assertFalse(looks_damaged(healthy))

    def test_no_broken_contraction_means_untouched(self):
        self.assertFalse(looks_damaged("A perfectly ordinary sentence."))

    def test_empty_input(self):
        self.assertFalse(looks_damaged(""))


class Repair(unittest.TestCase):
    def test_contractions_and_clitics(self):
        out, _ = restore_stripped_apostrophes(DAMAGED)
        self.assertEqual(
            out, "I don't know. It was Miller's house. I'm sure you're right.")

    def test_healthy_text_is_returned_byte_identical(self):
        out, changes = restore_stripped_apostrophes(HEALTHY)
        self.assertEqual(out, HEALTHY)
        self.assertEqual(changes, [])

    def test_hyphenated_word_is_not_split(self):
        """'once re-entered' must not become "once're-entered".

        Without the (?![-\\w]) lookahead this was two thirds of ALL false
        insertions across 18 novels - more than every other cause combined.
        """
        src = "He don t know; she once re-entered the pre-war house."
        out, _ = restore_stripped_apostrophes(src)
        self.assertIn("once re-entered", out)
        self.assertIn("pre-war", out)
        self.assertNotIn("once're", out)

    def test_length_is_preserved(self):
        """A space becomes an apostrophe; nothing is added or removed."""
        out, _ = restore_stripped_apostrophes(DAMAGED)
        self.assertEqual(len(out), len(DAMAGED))

    def test_changes_are_reported_with_locations(self):
        _, changes = restore_stripped_apostrophes(DAMAGED)
        self.assertTrue(changes)
        self.assertEqual(changes[0]["rule"], "stripped_apostrophe")
        self.assertIn("line", changes[0])


class Pipeline(unittest.TestCase):
    def _text(self, raw):
        out = get_preprocessed_source(raw, strip_front_matter=False)
        return out[0] if isinstance(out, tuple) else out

    def test_generation_preprocessing_repairs_damaged_source(self):
        self.assertIn("don't", self._text(DAMAGED))

    def test_generation_preprocessing_leaves_healthy_source_alone(self):
        self.assertEqual(self._text(HEALTHY), HEALTHY)


if __name__ == "__main__":
    unittest.main()
