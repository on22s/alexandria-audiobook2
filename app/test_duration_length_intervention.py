import unittest

from experiments.duration_length_intervention import build_short_pairs, summarize


class DurationLengthInterventionTests(unittest.TestCase):
    def test_pairs_are_deterministic_and_use_the_shortest_complete_rows(self):
        rows = [{"id": name, "text": text, "clone_wav": wav}
                for name, text, wav in (("c", "123", "c.wav"),
                                        ("a", "1", "a.wav"),
                                        ("b", "12", "b.wav"),
                                        ("x", "", None))]
        pairs = build_short_pairs(rows, 1)
        self.assertEqual(["a", "b"], [row["id"] for row in pairs[0]])

    def test_summary_measures_matched_improvement_toward_one(self):
        summary = summarize([
            {"separate_ratio": 0.7, "grouped_ratio": 0.9},
            {"separate_ratio": 1.1, "grouped_ratio": 1.3},
        ])
        self.assertEqual(1, summary["pairs_closer_to_one"])
        self.assertEqual(1, summary["pairs_farther_from_one"])


if __name__ == "__main__":
    unittest.main()
