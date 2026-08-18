import copy
import unittest

from speaker_identity import (build_speaker_consistency_report,
                              resolve_speaker_label,
                              stabilize_speaker_identities)


def _entry(speaker):
    return {"speaker": speaker, "text": "Spoken text.", "instruct": "Natural."}


class SpeakerIdentityTests(unittest.TestCase):

    def test_consistency_report_counts_usage_and_deduplicates_suggestions(self):
        entries = [{"speaker": "ROSWAAL"}, {"speaker": "ROSWAL"},
                   {"speaker": "ROSWAAL"}]
        review = [{"entry_number": 2, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
                  {"entry_number": 3, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]}]

        report = build_speaker_consistency_report(entries, review)

        self.assertEqual(2, report["speaker_count"])
        self.assertEqual(2, report["speakers"][0]["entry_count"])
        self.assertEqual(1, len(report["review_suggestions"]))
    def test_safe_variants_reuse_first_established_spelling_without_mutation(self):
        entries = [_entry(" roswaal "), _entry("ROSWAAL"), _entry("Voice O.S.")]
        original = copy.deepcopy(entries)

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "VOICE (O.S.)"])

        self.assertEqual(["ROSWAAL", "ROSWAAL", "VOICE (O.S.)"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(original, entries)

    def test_uncertain_typo_and_extended_name_are_reported_not_merged(self):
        entries = [_entry("ROSWAL"), _entry("OTTO SUWEN")]

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "OTTO"])

        self.assertEqual(["ROSWAL", "OTTO SUWEN"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(["ROSWAAL"],
                         [item["speaker"] for item in result["review"][0]["candidates"]])
        self.assertEqual(["OTTO"],
                         [item["speaker"] for item in result["review"][1]["candidates"]])

    def test_distinct_names_are_not_reported(self):
        result = stabilize_speaker_identities(
            [_entry("EMILIA"), _entry("VILLAGER 2"), _entry("SUBARU'S MOTHER")],
            ["SUBARU", "VILLAGER 1"])
        self.assertEqual([], result["review"])

    def test_resolve_speaker_label_matches_punctuation_and_spacing_variants(self):
        labels = ["MR. SMITH", "NARRATOR"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("MR SMITH", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("Mr.Smith", labels))

    def test_resolve_speaker_label_returns_none_when_no_match(self):
        self.assertIsNone(resolve_speaker_label("NOBODY", ["MR. SMITH", "NARRATOR"]))
        self.assertIsNone(resolve_speaker_label("", ["MR. SMITH"]))

    def test_resolve_speaker_label_is_deterministic_on_duplicate_keys(self):
        # Both labels normalize to the same identity key; sorted order picks
        # the same winner every time regardless of input iteration order.
        labels = ["Mr Smith", "MR. SMITH"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", list(reversed(labels))))


if __name__ == "__main__":
    unittest.main()


class SplitNameSpellingTest(unittest.TestCase):
    """The best-formed spelling wins, not the first-arrived one.

    THE BUG. Generating mushoku18, the model emitted `R UDEUS` 86 times and
    `RUDEUS` 16 - the name split after its first letter, which happened to
    exactly one character in three books (every other capitalised name came
    through whole). Both spellings normalise to the same identity, so they were
    already being merged; the canonical was simply whichever arrived first, and
    the split one arrived first. All 102 lines in that book and 137 in
    mushoku23 ended up attributed to a person no cast list contains, so they
    match no voice.
    """

    def test_the_intact_spelling_wins_even_when_the_split_one_comes_first(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        self.assertEqual(["RUDEUS", "RUDEUS"],
                         [e["speaker"] for e in out["entries"]])
        self.assertIn("RUDEUS", out["speakers"])
        self.assertNotIn("R UDEUS", out["speakers"])

    def test_a_genuinely_multi_word_name_is_left_alone(self):
        """The repair must not glue real names together: these have no
        alternative spelling, so there is nothing to prefer."""
        names = ["NORTH KING WII TAA", "OLD MAN", "KNUCKLE GUARD"]
        out = stabilize_speaker_identities([{"speaker": n, "text": "x"} for n in names])
        self.assertEqual(names, [e["speaker"] for e in out["entries"]])

    def test_the_roster_still_wins_over_both_spellings(self):
        # An explicit cast list is the caller's own answer and outranks
        # anything inferred from how the model happened to write it.
        out = stabilize_speaker_identities(
            [{"speaker": "R UDEUS", "text": "a"}, {"speaker": "RUDEUS", "text": "b"}],
            established_speakers=["Rudeus"])
        self.assertEqual(["Rudeus", "Rudeus"], [e["speaker"] for e in out["entries"]])

    def test_the_change_is_recorded_so_it_can_be_audited(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        kinds = {c["type"] for c in out["changes"]}
        self.assertIn("speaker_spelling", kinds)


class FinishedScriptRepairTest(unittest.TestCase):
    """A script already saved with the broken name must still be repairable.

    Preferring the better spelling only helps while both exist. In mushoku18's
    saved script they do not: generation had already collapsed `RUDEUS` onto
    `R UDEUS` line by line, leaving 67 of the broken form and none of the good
    one. The book's own prose is the evidence - it says "Rudeus" 123 times and
    "R udeus" never.
    """

    def test_a_split_name_is_rejoined_when_the_prose_confirms_it(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "『Ariel-sama.』"},
            {"speaker": "NARRATOR", "text": "Rudeus turned to look at her."},
        ])
        self.assertEqual("RUDEUS", out["entries"][0]["speaker"])

    def test_nothing_is_joined_without_that_evidence(self):
        """`J SMITH` is a person, not a typo. Guessing invents a character."""
        out = stabilize_speaker_identities([
            {"speaker": "J SMITH", "text": "He said nothing at all."},
        ])
        self.assertEqual("J SMITH", out["entries"][0]["speaker"])

    def test_a_two_word_name_is_not_glued_together(self):
        out = stabilize_speaker_identities([
            {"speaker": "OLD MAN", "text": "The old man walked away."},
        ])
        self.assertEqual("OLD MAN", out["entries"][0]["speaker"])

    def test_the_repair_is_recorded(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "Rudeus said so himself."},
        ])
        self.assertIn("speaker_split_repair", {c["type"] for c in out["changes"]})
