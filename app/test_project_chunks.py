import unittest

from project import get_speakable_entries, group_into_chunks
from tts import DEFAULT_PAUSE_MS


class SpeakableEntryTests(unittest.TestCase):
    def test_nonverbal_dialogue_becomes_pause_without_mutating_input(self):
        entries = [
            {"speaker": "A", "text": "Wait here.", "instruct": "quiet"},
            {"speaker": "A", "text": "――――", "instruct": "silent"},
            {"speaker": "B", "text": "I understand.", "instruct": "calm"},
        ]
        prepared = get_speakable_entries(entries)
        self.assertEqual(["Wait here.", "I understand."],
                         [entry["text"] for entry in prepared])
        self.assertEqual(DEFAULT_PAUSE_MS, prepared[0]["pause_after"])
        self.assertNotIn("pause_after", entries[0])

    def test_block_glyphs_and_leading_marks_are_not_sent_to_tts(self):
        entries = [
            {"speaker": "A", "text": "…", "instruct": "silent"},
            {"speaker": "A", "text": "■■●■", "instruct": "noise"},
        ]
        self.assertEqual([], group_into_chunks(entries))

    def test_spoken_words_with_punctuation_remain_speakable(self):
        entries = [{"speaker": "A", "text": "No—wait!", "instruct": "urgent"}]
        self.assertEqual("No—wait!", group_into_chunks(entries)[0]["text"])


if __name__ == "__main__":
    unittest.main()
