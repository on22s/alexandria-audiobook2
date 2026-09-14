import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments"))
from dracor_prose import display_name, frame_row  # noqa: E402

ROW = {"roster": ["ADELBERT", "MRS. BENNET"], "teacher": "ADELBERT",
       "context": [{"type": "SPOKEN", "text": "Where is he?"},
                   {"type": "SPOKEN", "text": "I fear him not.", "target": True},
                   {"type": "SPOKEN", "text": "Then stay."}],
       "line": "I fear him not.", "context_speakers": ["MRS. BENNET", "MRS. BENNET"],
       "book": "dracor_am_x", "language": "en"}


class Frames(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(display_name("MRS. BENNET"), "Mrs. Bennet")

    def test_target_line_and_label_never_change(self):
        for seed in range(50):
            out = frame_row(ROW, random.Random(seed))
            self.assertEqual(out["context"][1], ROW["context"][1])
            self.assertEqual(out["teacher"], "ADELBERT")
            self.assertEqual(out["line"], "I fear him not.")

    def test_frames_name_the_right_speaker(self):
        seen = set()
        for seed in range(200):
            out = frame_row(ROW, random.Random(seed))
            f = out["frames"]
            seen.add((f["target"], f["previous"], f["next"]))
            prev, nxt = out["context"][0], out["context"][2]
            if f["previous"] == "own_beat":
                self.assertEqual(prev["type"], "NARRATOR"); self.assertIn("Adelbert", prev["text"])
            if f["previous"] == "previous_speaker_post":
                self.assertIn("Mrs. Bennet", prev["text"]); self.assertNotIn("Adelbert", prev["text"])
            if f["next"] == "own_post":
                self.assertEqual(nxt["type"], "NARRATOR"); self.assertIn("Adelbert", nxt["text"])
            if f["next"] == "next_speaker_beat":
                self.assertIn("Mrs. Bennet", nxt["text"])
            if f["previous"] == "line":
                self.assertEqual(prev, ROW["context"][0])
        # every confusable shape gets exercised
        self.assertTrue(any(s[1] == "previous_speaker_post" for s in seen))
        self.assertTrue(any(s[2] == "next_speaker_beat" for s in seen))
        self.assertTrue(any(s[0] == "none" for s in seen))

    def test_stage_direction_neighbour_is_kept_not_framed(self):
        row = dict(ROW, context=[{"type": "NARRATOR", "text": "Exit Huon."}, ROW["context"][1], ROW["context"][2]],
                   context_speakers=[None, "MRS. BENNET"])
        for seed in range(50):
            out = frame_row(row, random.Random(seed))
            if out["frames"]["target"] != "pre":
                self.assertEqual(out["context"][0]["text"], "Exit Huon.")

    def test_seeded_and_deterministic(self):
        self.assertEqual(frame_row(ROW, random.Random(7)), frame_row(ROW, random.Random(7)))


if __name__ == "__main__":
    unittest.main()
