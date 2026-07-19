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
