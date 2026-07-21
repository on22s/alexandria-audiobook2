import unittest
import three_pass_generate as tp


class PassHelperTests(unittest.TestCase):
    def test_batches_split_entries_by_size(self):
        entries = [{"text": str(i)} for i in range(55)]
        batches = list(tp.iter_entry_batches(entries, batch_size=25))
        self.assertEqual([25, 25, 5], [len(b) for b in batches])

    def test_roster_collects_uppercase_non_narrator_speakers(self):
        entries = [{"speaker": "NARRATOR"}, {"speaker": "ELENA"},
                   {"speaker": "MARCUS"}, {"speaker": "ELENA"}, {"speaker": "UNKNOWN"}]
        self.assertEqual(["ELENA", "MARCUS"], tp.build_roster(entries))

    def test_default_instruct_by_type(self):
        self.assertEqual("Neutral, even narration.",
                         tp.default_instruct({"speaker": "NARRATOR", "text": "x"}))
        self.assertEqual("Natural, in-character delivery.",
                         tp.default_instruct({"speaker": "ELENA", "text": "x"}))


if __name__ == "__main__":
    unittest.main()
