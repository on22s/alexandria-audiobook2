"""#628: a model shown one paragraph per line returned the line break between
two quotations as {"type": "NARRATOR", "text": "\\n"}, and the gate's
empty_text finding rejected the whole reply. The unit is dropped only when the
source between its neighbours is itself just whitespace and quote marks -
finding #7 (an empty unit may be a LOST line and must reach the gate) is
kept for every other case."""
import unittest

from three_pass_generate import drop_whitespace_entries, _call_segment
from pass_quality import validate_segment_quality
import default_prompts

CHUNK = ('Everyone stared at the result.\n“Grade A+? Wow.”\n“Is she really a first-year like us?”\n'
         '“Are we really the same age?”\nThe professor cleared his throat.')
REPLY = [{"type": "NARRATOR", "text": "Everyone stared at the result."},
         {"type": "SPOKEN", "text": "Grade A+? Wow."},
         {"type": "NARRATOR", "text": "\n"},
         {"type": "SPOKEN", "text": "Is she really a first-year like us?"},
         {"type": "NARRATOR", "text": "\n"},
         {"type": "SPOKEN", "text": "Are we really the same age?"},
         {"type": "NARRATOR", "text": "The professor cleared his throat."}]


class DropWhitespaceEntries(unittest.TestCase):
    def test_line_break_units_between_quotations_are_dropped(self):
        kept, dropped = drop_whitespace_entries(REPLY, CHUNK)
        self.assertEqual([e["text"] for e in REPLY if e["text"].strip()], [e["text"] for e in kept])
        self.assertEqual([3, 5], [d["entry_number"] for d in dropped])
        self.assertTrue(all(d["code"] == "dropped_whitespace_entry" for d in dropped))

    def test_an_empty_unit_standing_for_real_text_is_kept_for_the_gate(self):
        # The model dropped the middle line and left an empty unit in its
        # place: the source between the neighbours has words, so it stays and
        # validate_segment_quality flags it (finding #7).
        reply = [{"type": "SPOKEN", "text": "Grade A+? Wow."},
                 {"type": "SPOKEN", "text": ""},
                 {"type": "SPOKEN", "text": "Are we really the same age?"}]
        kept, dropped = drop_whitespace_entries(reply, CHUNK)
        self.assertEqual(3, len(kept)); self.assertEqual([], dropped)
        report = validate_segment_quality(CHUNK, kept)
        self.assertIn("empty_text", {f["code"] for f in report["findings"]})

    def test_unlocatable_neighbours_keep_the_unit(self):
        reply = [{"type": "SPOKEN", "text": "Not in the chunk at all."},
                 {"type": "NARRATOR", "text": " "},
                 {"type": "SPOKEN", "text": "Nor this."}]
        kept, dropped = drop_whitespace_entries(reply, CHUNK)
        self.assertEqual(3, len(kept)); self.assertEqual([], dropped)

    def test_the_issue_reply_passes_the_gate_after_the_drop(self):
        kept, _ = drop_whitespace_entries(REPLY, CHUNK)
        report = validate_segment_quality(CHUNK, kept)
        self.assertTrue(report["passed"], report["findings"])
        self.assertFalse(validate_segment_quality(CHUNK, REPLY)["passed"])

    def test_prompt_says_never_emit_an_empty_entry(self):
        system, _ = default_prompts.load_segment_prompts()
        self.assertIn("Never emit an entry with no words", system)


if __name__ == "__main__":
    unittest.main()
