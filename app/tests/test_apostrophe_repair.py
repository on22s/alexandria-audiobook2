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
EXPECTED_CONTRACTION_STEMS = (
    "don", "isn", "didn", "doesn", "wasn", "weren", "hasn", "haven",
    "hadn", "couldn", "wouldn", "shouldn", "mustn", "needn", "aren",
    "can", "won", "ain", "shan", "mightn", "daren", "oughtn", "mayn",
)


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

    def test_curly_and_mixed_apostrophe_counts(self):
        """Curly apostrophes count alongside straight ones toward the healthy ratio."""
        curly_healthy = "She didn\u2019t know it\u2019s late, and he wasn\u2019t sure. I don t mind."
        self.assertFalse(looks_damaged(curly_healthy))
        self.assertEqual(restore_stripped_apostrophes(curly_healthy), (curly_healthy, []))

        curly_damaged = "He said it\u2019s fine, she\u2019s here. I don t know."
        self.assertTrue(looks_damaged(curly_damaged))
        repaired_curly, curly_changes = restore_stripped_apostrophes(curly_damaged)
        self.assertEqual(repaired_curly, "He said it\u2019s fine, she\u2019s here. I don't know.")
        self.assertEqual(len(curly_changes), 1)

        # Two straight + one curly exceeds the floor of 2 only when summed (not subtracted).
        mixed_healthy = "It's fine and she's ready, though he wasn\u2019t sure. I don t know."
        self.assertFalse(looks_damaged(mixed_healthy))
        self.assertEqual(restore_stripped_apostrophes(mixed_healthy), (mixed_healthy, []))

        mixed_damaged = "It's fine and she\u2019s ready. I don t know."
        self.assertTrue(looks_damaged(mixed_damaged))

    def test_floor_and_ratio_boundaries(self):
        """Pin both sides of max(2, broken * 0.05) at the floor and above it."""
        # Floor boundary (broken = 1 -> max(2, 0.05) = 2): 2 is damaged, 3 is healthy.
        self.assertTrue(looks_damaged("It's fine, she's here. I don t know."))
        self.assertFalse(looks_damaged("It's fine, she's here, he's ready. I don t know."))

        # Ratio boundary (broken = 60 -> max(2, 3.0) = 3.0): 3 is damaged, 4 is healthy.
        breaks = "I don t know. " * 60
        self.assertTrue(looks_damaged("It's fine, she's here, he's ready. " + breaks))
        self.assertFalse(
            looks_damaged("It's fine, she's here, he's ready, we're set. " + breaks)
        )


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

    def test_all_contraction_stems_are_detected_and_repaired(self):
        """Every supported negative contraction stem detects damage and repairs cleanly."""
        for stem in EXPECTED_CONTRACTION_STEMS:
            with self.subTest(stem=stem):
                damaged = f"They {stem} t know."
                expected = f"They {stem}'t know."
                self.assertTrue(looks_damaged(damaged))
                repaired, changes = restore_stripped_apostrophes(damaged)
                self.assertEqual(repaired, expected)
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["before"], f"{stem} t")
                self.assertEqual(changes[0]["after"], f"{stem}'t")
                self.assertEqual(changes[0]["rule"], "stripped_apostrophe")
                self.assertFalse(looks_damaged(expected))
                self.assertEqual(restore_stripped_apostrophes(expected), (expected, []))

    def test_leading_newline_offsets_and_line_numbers(self):
        """A leading newline must be counted when computing 1-based line numbers."""
        raw = "\nI don t know.\nShe s here."
        repaired, changes = restore_stripped_apostrophes(raw)
        self.assertEqual(repaired, "\nI don't know.\nShe's here.")
        self.assertEqual(
            changes,
            [
                {
                    "offset": raw.index("don t"),
                    "line": 2,
                    "before": "don t",
                    "after": "don't",
                    "rule": "stripped_apostrophe",
                },
                {
                    "offset": raw.index("She s"),
                    "line": 3,
                    "before": "She s",
                    "after": "She's",
                    "rule": "stripped_apostrophe",
                },
            ],
        )


class Pipeline(unittest.TestCase):
    def _text(self, raw):
        out = get_preprocessed_source(raw, strip_front_matter=False)
        return out[0] if isinstance(out, tuple) else out

    def test_generation_preprocessing_repairs_damaged_source(self):
        self.assertIn("don't", self._text(DAMAGED))

    def test_generation_preprocessing_leaves_healthy_source_alone(self):
        self.assertEqual(self._text(HEALTHY), HEALTHY)




class CliticAuditEvidenceTests(unittest.TestCase):
    def test_each_actual_repair_has_original_offset_and_survives_pipeline_reporting(self):
        import json
        import tempfile
        from pathlib import Path
        raw = "I m here. I don t know.\nWe re home and she s tired.\nThey ve gone, you ll stay, I d wait. once re-entered."
        expected = "I'm here. I don't know.\nWe're home and she's tired.\nThey've gone, you'll stay, I'd wait. once re-entered."
        repaired, changes = restore_stripped_apostrophes(raw)
        self.assertEqual(expected, repaired)
        self.assertEqual(7, len(changes))
        replay = raw
        for change in sorted(changes, key=lambda item: item["offset"], reverse=True):
            offset = change["offset"]
            self.assertEqual(change["before"], raw[offset:offset + len(change["before"])])
            self.assertEqual(raw.count("\n", 0, offset) + 1, change["line"])
            self.assertEqual("stripped_apostrophe", change["rule"])
            replay = replay[:offset] + change["after"] + replay[offset + len(change["before"]):]
        self.assertEqual(repaired, replay)
        processed, report = get_preprocessed_source(raw, strip_front_matter=False)
        self.assertEqual(expected, processed)
        self.assertEqual(7, sum(change["rule"] == "stripped_apostrophe"
                                for change in report["source_normalizations"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_audit.json"
            path.write_text(json.dumps({"source": raw, "repaired": processed, "report": report}))
            actual = json.loads(path.read_text())
            self.assertEqual(expected, actual["repaired"])
            self.assertEqual(7, len(actual["report"]["source_normalizations"]))

    def test_healthy_clitics_and_hyphenated_words_remain_unchanged(self):
        raw = "I'm here and we're ready. once re-entered."
        self.assertEqual((raw, []), restore_stripped_apostrophes(raw))


if __name__ == "__main__":
    unittest.main()
