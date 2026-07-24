import unittest

from compare_attribution_arms import find_disagreements, sample_disagreements


class FindDisagreementsTest(unittest.TestCase):

    def test_matching_arms_have_no_disagreements(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"},
                 {"speaker": "HACHIKUJI", "text": "Bye"}]
        self.assertEqual(find_disagreements(arm_a, list(arm_a)), [])

    def test_differing_speaker_is_reported(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [{"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 0)
        self.assertEqual(found[0]["arm_a"], "ARARAGI")
        self.assertEqual(found[0]["arm_b"], "HANEKAWA")
        self.assertEqual(found[0]["text"], "Hi")

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            find_disagreements([{"speaker": "A", "text": "x"}], [])

    def test_null_entries_are_skipped(self):
        arm_a = [None, {"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [None, {"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 1)


class SampleDisagreementsTest(unittest.TestCase):

    def test_sample_is_deterministic_for_a_seed(self):
        rows = [{"index": i, "arm_a": "A", "arm_b": "B", "text": str(i)}
                for i in range(200)]
        first = sample_disagreements(rows, size=50, seed=7)
        second = sample_disagreements(rows, size=50, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)

    def test_sample_smaller_than_size_returns_all(self):
        rows = [{"index": 0, "arm_a": "A", "arm_b": "B", "text": "x"}]
        self.assertEqual(len(sample_disagreements(rows, size=50, seed=7)), 1)


if __name__ == "__main__":
    unittest.main()
