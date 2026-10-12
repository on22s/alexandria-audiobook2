"""TTS chunks never open on separator punctuation (#1121, #1122).

WHAT THIS VERIFIES. A narration entry after a closing quote starts ", holding
her hand…"; sent to TTS as-is it garbled at 2 of 5 seeds (2026-10-12). Here:
the chunk text loses its leading separators while speakers, instructs, pauses
and source-entry indices are unchanged; a merged entry that starts with
punctuation joins without a stray space; separator-only text is left alone; and
delivery retry still matches a chunks.json saved with the old text. All text is
synthetic.
"""
import unittest

import project
from delivery_review import get_delivery_retry_chunks

SCRIPT = [
    {"speaker": "MARA", "text": "Wait,", "instruct": "Urgent."},
    {"speaker": "NARRATOR", "text": ", she said, pulling on her coat.", "instruct": "Neutral."},
    {"speaker": "MARA", "text": "Now.", "instruct": "Urgent."},
    {"speaker": "NARRATOR", "text": "; for the tide was turning.", "instruct": "Neutral.",
     "pause_after": 900},
]


class ChunkTextTest(unittest.TestCase):
    def test_leading_separators_are_dropped_from_tts_text(self):
        chunks = project.group_into_chunks(SCRIPT, include_source_indices=True)
        self.assertEqual(["Wait,", "she said, pulling on her coat.", "Now.", "for the tide was turning."],
                         [c["text"] for c in chunks])

    def test_everything_but_the_text_is_unchanged(self):
        def without_text(chunks):
            return [{k: v for k, v in c.items() if k != "text"} for c in chunks]
        old = [{**e, "text": e["text"].lstrip(",; ")} for e in SCRIPT]
        self.assertEqual(without_text(project.group_into_chunks(old, include_source_indices=True)),
                         without_text(project.group_into_chunks(SCRIPT, include_source_indices=True)))
        self.assertEqual(900, project.group_into_chunks(SCRIPT)[-1]["pause_after"])

    def test_merged_entry_starting_with_punctuation_joins_without_a_space(self):
        clause = "She stood at the open door for a long while, watching the fog roll in over the water"
        script = [{"speaker": "NARRATOR", "text": clause, "instruct": "Neutral."},
                  {"speaker": "NARRATOR", "text": ", then she left.", "instruct": "Neutral."}]
        self.assertEqual([clause + ", then she left."],
                         [c["text"] for c in project.group_into_chunks(script)])

    def test_separator_only_text_is_left_alone(self):
        self.assertEqual(", ;", project.get_tts_chunk_text(", ;"))
        self.assertEqual("Plain text.", project.get_tts_chunk_text("Plain text."))


class DeliveryRetryCompatibilityTest(unittest.TestCase):
    def test_chunks_saved_before_the_change_still_match(self):
        entries = [{**e, "instruct_unchecked": e["speaker"] == "MARA"} for e in SCRIPT]
        saved = [{**c, "id": i, "uid": f"u{i}", "status": "done", "audio_path": f"a{i}.wav"}
                 for i, c in enumerate(project.group_into_chunks(SCRIPT))]
        saved[1]["text"] = ", she said, pulling on her coat."  # as chunked before #1121
        saved[3]["text"] = "; for the tide was turning."
        after = [{**e, "instruct": "Calm."} if e["speaker"] == "MARA" else e for e in entries]
        rows = get_delivery_retry_chunks(entries, after, saved)
        self.assertEqual(len(saved), len(rows))
        self.assertEqual("u1", rows[1]["uid"])  # unaffected narrator chunk kept with its audio

    def test_a_real_editor_text_change_is_still_refused(self):
        saved = [{**c, "id": i, "uid": f"u{i}"} for i, c in enumerate(project.group_into_chunks(SCRIPT))]
        saved[1]["text"] = "she whispered, pulling on her coat."
        with self.assertRaises(ValueError):
            get_delivery_retry_chunks(SCRIPT, SCRIPT, saved)


if __name__ == "__main__":
    unittest.main()
