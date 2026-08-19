"""Two changes aimed at one measured failure.

On two books the pipeline had never seen, roughly 70-78% of spoken lines were
left attributed to NARRATOR, and 85% of those had the speaker named in an
adjacent line - the information was present and unused.

1. WHO NARRATES. On PDNC gold, telling the model the first-person narrator's
   identity took attribution from 61.7% to 79.4% over 720 rows
   (pdnc_narrator_prior__clean-3book.json). narrator_prompt.py has carried that
   wording since; generate_script never called it.

2. A WAY TO SAY "I CANNOT TELL". The prompt offered no label for an
   unidentifiable speaker, so uncertainty and narration shared one word.
   That makes the failure invisible AND unfixable: nothing downstream can tell
   a line that is narration from a line whose speaker was simply not resolved.
"""
import os
import re
import unittest

from narrator_prompt import (add_first_person_awareness, add_narrator_prior,
                             get_valid_narrator_name, is_narrator_attested)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class NarratorPriorTest(unittest.TestCase):
    def test_the_prompt_names_the_narrator(self):
        out = add_narrator_prior("BASE RULES", "Rudeus")
        self.assertIn("RUDEUS", out)
        self.assertIn("first person", out)
        self.assertTrue(out.startswith("BASE RULES"))

    def test_a_narrator_who_is_not_in_the_book_is_rejected(self):
        """A narrator absent from their own book is a typo or the wrong book,
        and seeding the roster from it teaches a character who is not there."""
        book = "Rudeus turned. Rudeus spoke. Rudeus waited by the door."
        self.assertTrue(is_narrator_attested("Rudeus", book))
        self.assertFalse(is_narrator_attested("Sylphiette", book))

    def test_narrator_and_unknown_are_not_valid_narrator_names(self):
        for bad in ("NARRATOR", "UNKNOWN"):
            with self.assertRaises(ValueError):
                get_valid_narrator_name(bad)

    def test_first_person_awareness_allows_the_narrator_to_speak(self):
        # The failure being fixed: in a first-person book the narrator also
        # speaks aloud, and those lines belong to the character, not to
        # NARRATOR.
        out = add_first_person_awareness("BASE")
        self.assertIn("speak", out.lower())


class UnknownSpeakerPromptTest(unittest.TestCase):
    """The shipped prompt must offer UNKNOWN, and must not blur it with
    NARRATOR - the whole value is that they are different findings."""

    def setUp(self):
        with open(os.path.join(REPO, "default_prompts.txt"), encoding="utf-8") as fh:
            self.prompt = fh.read()

    def test_the_prompt_offers_unknown_for_unidentifiable_speech(self):
        self.assertIn("UNKNOWN", self.prompt)

    def test_it_forbids_using_narrator_for_that_case(self):
        rule = next(line for line in self.prompt.splitlines()
                    if "cannot tell who" in line)
        self.assertIn("never NARRATOR", rule)

    def test_it_distinguishes_the_two_meanings(self):
        self.assertRegex(self.prompt,
                         r"NARRATOR means .*narration.*\n.*UNKNOWN means|"
                         r'UNKNOWN means "this is speech')
