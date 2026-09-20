"""Issues #622, #623, #624 (REFLEXGAMING007, 2026-09-20), manual transport with
ChatGPT as the model. Each fixture is the reply from the issue, so the tests
pin the exact accept set the user hit, not a paraphrase of it."""
import unittest

from pass_quality import (analyze_outer_quote_regions, index_head_check,
                          is_attested_name, validate_attribution,
                          validate_segment_quality)
from attribution_prompt_variants import MICHEL2_SYSTEM


class IndexAsString623(unittest.TestCase):
    """#623: the reply carried n as "8" not 8. The prompt never said integer;
    a digit string is unambiguous and is read as its integer."""
    FROZEN = [{"type": "NARRATOR", "text": "He sat."},
              {"type": "SPOKEN", "text": "Grade E?"}]

    def test_digit_string_index_is_accepted(self):
        ok, reason, ordered = index_head_check(
            self.FROZEN, [{"n": "0", "speaker": "NARRATOR"}, {"n": "1", "speaker": "ISAAC"}])
        self.assertTrue(ok, reason)
        self.assertEqual(["NARRATOR", "ISAAC"], [o["speaker"] for o in ordered])

    def test_padded_and_reordered_digit_strings_still_bind_by_index(self):
        ok, _, ordered = index_head_check(
            self.FROZEN, [{"n": " 1 ", "speaker": "ISAAC"}, {"n": "0", "speaker": "NARRATOR"}])
        self.assertTrue(ok)
        self.assertEqual("ISAAC", ordered[1]["speaker"])

    def test_non_numeric_strings_are_still_rejected(self):
        for bad in ("one", "", "1.5", "-1", "0x1", None):
            ok, reason, _ = index_head_check(
                self.FROZEN, [{"n": bad, "speaker": "NARRATOR"}, {"n": 1, "speaker": "ISAAC"}])
            self.assertFalse(ok, bad)
            self.assertIn("index", reason)

    def test_prompt_states_the_integer_and_shows_one(self):
        self.assertIn('{"n": 0, "speaker": "NARRATOR"}', MICHEL2_SYSTEM)
        self.assertIn("integer", MICHEL2_SYSTEM)


class FullNameAttested622(unittest.TestCase):
    """#622: "IAN FAIRYTALE" rejected while "IAN" passed. The book writes the
    full name once (his introduction) and the first name throughout. A
    multi-word name whose full form is in the text and whose first word is
    attested on its own is that character, not an invention."""
    BOOK = ("Ian said nothing. " * 40 + "Ian Fairytale, who would also be graded "
            "at Grade E, was able to enroll thanks to his rarity. " + "Ian laughed. " * 40
            + "The professor spoke to the class about the future. " * 120)

    def test_full_name_written_once_with_attested_first_name_passes(self):
        self.assertTrue(is_attested_name("IAN FAIRYTALE", self.BOOK))
        self.assertTrue(is_attested_name("IAN", self.BOOK))

    def test_an_invented_full_name_is_still_rejected(self):
        # "future" is a word the book uses 60 times, lowercase; "FUTURE ME"
        # appears nowhere as a name.
        self.assertFalse(is_attested_name("FUTURE ME", self.BOOK))
        # first word attested, full form never written: still an invention
        self.assertFalse(is_attested_name("IAN HUMPHREY", self.BOOK))

    def test_gate_accepts_the_issue_reply(self):
        frozen = [{"type": "SPOKEN", "text": "My dream is not to be a scholar!"}]
        report = validate_attribution(frozen, [{"n": 0, "speaker": "IAN FAIRYTALE"}],
                                      source_text=self.BOOK)
        self.assertTrue(report["passed"], report["findings"])

    def test_rejection_message_names_the_threshold(self):
        frozen = [{"type": "SPOKEN", "text": "My dream is not to be a scholar!"}]
        report = validate_attribution(frozen, [{"n": 0, "speaker": "FUTURE ME"}],
                                      source_text=self.BOOK)
        self.assertFalse(report["passed"])
        msg = report["findings"][0]["message"]
        self.assertIn("twice", msg)


class RunOnQuote624(unittest.TestCase):
    """#624: the source drops a closing quote, so the next opening quote was
    read as a nested one; everything to the following closer became SPOKEN
    and the narration entry between was rejected — while the wrong labelling
    (narration as SPOKEN) passed. A second opening quote after a finished
    sentence is a new speech, and the missing closer is inferred there."""
    SOURCE = ("“As you can see, the mana meter is working just fine. According to the "
              "results of the inspection this morning, there were no abnormalities in "
              "the device. At the very least, there has never been a case of an incorrect "
              "measurement. If an error occurs, the operation itself won’t work because "
              "of the device’s structure. “…Is that so.” Kaya now had an expression of "
              "disappointment on her face, as if Professor Fernando’s answer wasn’t the "
              "one she wanted to hear. Seeing the reaction he was given, Professor "
              "Fernando then proceeded to explain the second possible explanation, "
              "“…Of course, there are some cases where the evaluation ends up being "
              "wrong even without an error. Rare, but possible.” Professor Fernando "
              "suddenly made an unfamiliar argument, confusing the students listening.")
    REPLY = [
        {"type": "SPOKEN", "text": "As you can see, the mana meter is working just fine. According to the results of the inspection this morning, there were no abnormalities in the device. At the very least, there has never been a case of an incorrect measurement. If an error occurs, the operation itself won’t work because of the device’s structure."},
        {"type": "SPOKEN", "text": "…Is that so."},
        {"type": "NARRATOR", "text": "Kaya now had an expression of disappointment on her face, as if Professor Fernando’s answer wasn’t the one she wanted to hear. Seeing the reaction he was given, Professor Fernando then proceeded to explain the second possible explanation,"},
        {"type": "SPOKEN", "text": "…Of course, there are some cases where the evaluation ends up being wrong even without an error. Rare, but possible."},
        {"type": "NARRATOR", "text": "Professor Fernando suddenly made an unfamiliar argument, confusing the students listening."},
    ]

    def test_regions_split_at_the_run_on_and_record_the_repair(self):
        analysis = analyze_outer_quote_regions(self.SOURCE)
        types = [r["type"] for r in analysis["regions"]]
        self.assertEqual(["SPOKEN", "SPOKEN", "NARRATOR", "SPOKEN", "NARRATOR"], types)
        self.assertIn("inferred_missing_close_quote",
                      {r["code"] for r in analysis["repairs"]})

    def test_the_correct_reply_passes(self):
        report = validate_segment_quality(self.SOURCE, self.REPLY)
        self.assertTrue(report["passed"], report["findings"])

    def test_narration_labelled_spoken_is_now_rejected(self):
        wrong = [dict(e) for e in self.REPLY]
        wrong[2]["type"] = "SPOKEN"
        report = validate_segment_quality(self.SOURCE, wrong)
        self.assertFalse(report["passed"])

    def test_a_real_nested_quote_is_still_nested(self):
        nested = "“He told me “never again” and left,” she said. Then silence."
        types = [r["type"] for r in analyze_outer_quote_regions(nested)["regions"]]
        self.assertEqual(["SPOKEN", "NARRATOR"], types)


if __name__ == "__main__":
    unittest.main()
