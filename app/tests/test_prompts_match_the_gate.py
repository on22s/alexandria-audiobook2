"""Issues #609, #610, #616: the pass-1 prompt told the model to TTS-normalise
while pass-1's gate demands the source verbatim, and left unsaid that the
SPOKEN/NARRATOR split is checked mechanically against the quote marks; the
pass-2 michel2 prompt opened as a spoken-line task and models dropped the
narration entries. These pin the prompts to what the gates accept."""
import os
import unittest

from pass_quality import validate_segment_quality

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Pass1PromptMatchesItsGate(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "default_prompts_segment.txt"), encoding="utf-8") as h:
            self.prompt = h.read()

    def test_the_prompt_no_longer_asks_for_a_rewrite_the_gate_rejects(self):
        for normalisation in ('"Chapter I" -> "Chapter One"', '"3rd" -> "third"', '"Dr." -> "Doctor"',
                              "TTS-normalize:"):
            self.assertNotIn(normalisation, self.prompt)
        self.assertIn("checked word for word against the source", self.prompt)

    def test_the_prompt_says_the_split_is_mechanical_and_names_both_traps(self):
        self.assertIn("quoted name, term or title", self.prompt)
        self.assertIn("printed without quotes is NARRATOR", self.prompt)

    def test_the_gate_rejects_the_normalised_reply_and_accepts_the_verbatim_one(self):
        # #610's shape: an ordinal expanded by the model.
        source = ('After each person, the professor read the results. '
                  '"Provisional 3rd Class. 1st, Grade C-. 2nd, Grade C+. 3rd, Grade C-." '
                  'With that said, the evaluation continued. Haaaaaah! she screamed.')
        verbatim = [{"type": "NARRATOR", "text": "After each person, the professor read the results."},
                    {"type": "SPOKEN", "text": "Provisional 3rd Class. 1st, Grade C-. 2nd, Grade C+. 3rd, Grade C-."},
                    {"type": "NARRATOR", "text": "With that said, the evaluation continued. Haaaaaah! she screamed."}]
        self.assertTrue(validate_segment_quality(source, verbatim)["passed"])
        # the unquoted scream tagged SPOKEN is exactly what the gate refuses
        split_scream = verbatim[:2] + [{"type": "NARRATOR", "text": "With that said, the evaluation continued."},
                                       {"type": "SPOKEN", "text": "Haaaaaah!"},
                                       {"type": "NARRATOR", "text": "she screamed."}]
        report = validate_segment_quality(source, split_scream)
        self.assertFalse(report["passed"])
        self.assertTrue({"quote_region_misclassified", "crosses_quote_boundary"} & {f["code"] for f in report["findings"]},
                        report["findings"])

    def test_a_quoted_term_must_be_its_own_spoken_entry(self):
        # #609's shape: the gate splits on the marks, so a quoted term is SPOKEN.
        source = 'That aura would leave something known as a "Mana Trail". It was evidence.'
        as_spoken = [{"type": "NARRATOR", "text": "That aura would leave something known as a"},
                     {"type": "SPOKEN", "text": "Mana Trail"},
                     {"type": "NARRATOR", "text": ". It was evidence."}]
        self.assertTrue(validate_segment_quality(source, as_spoken)["passed"])
        merged = [{"type": "NARRATOR", "text": 'That aura would leave something known as a "Mana Trail". It was evidence.'}]
        report = validate_segment_quality(source, merged)
        self.assertFalse(report["passed"])
        self.assertIn("mixed_quote_region", {f["code"] for f in report["findings"]})


class Pass2PromptLabelsEveryEntry(unittest.TestCase):
    def test_michel2_opens_as_an_every_entry_task_and_names_narration_first(self):
        from attribution_prompt_variants import MICHEL2_SYSTEM
        opening = MICHEL2_SYSTEM.split("\n")[0]
        self.assertTrue(opening.startswith("You label EVERY marked entry"), opening)
        self.assertLess(opening.index("NARRATOR"), opening.index("roster name"),
                        "narration must be named before the spoken-line rule, not after it")
        self.assertIn("narration entries included", MICHEL2_SYSTEM)
        self.assertNotIn("assign speaker names to the spoken lines", MICHEL2_SYSTEM)


if __name__ == "__main__":
    unittest.main()
