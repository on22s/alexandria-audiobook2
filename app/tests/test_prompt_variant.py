"""The control arm must be byte-identical to the shipped prompt.

The whole experiment is one sentence. If the control drifts by so much as a
newline, the difference measured is the refactor rather than the sentence -
the same trap `add_narrator_prior` was extracted to avoid (Rule 15).
"""
import unittest

from experiments.two_stage_attribution import (EXPLICIT_HINT, PROMPT_VARIANTS,
                                               build_prompt)

ENTRY = {"line": "The opposite?", "prev_context": "before", "next_context": "after"}
ROSTER = ["SUBARU", "SATELLA"]


class PromptVariantTest(unittest.TestCase):
    def test_control_is_the_default_and_adds_nothing(self):
        self.assertEqual(build_prompt(ENTRY, ROSTER),
                         build_prompt(ENTRY, ROSTER, variant="control"))

    def test_the_hint_is_a_pure_suffix_of_the_control(self):
        control = build_prompt(ENTRY, ROSTER)
        hinted = build_prompt(ENTRY, ROSTER, variant="explicit_hint")
        self.assertTrue(hinted.startswith(control))
        self.assertEqual(EXPLICIT_HINT, hinted[len(control):])

    def test_an_unknown_variant_is_refused_not_silently_treated_as_control(self):
        """A typo'd arm name must not quietly produce a second control run."""
        with self.assertRaises(ValueError):
            build_prompt(ENTRY, ROSTER, variant="explict_hint")

    def test_the_hint_survives_alongside_the_narrator_prior(self):
        """Both are appended; neither may swallow the other."""
        both = build_prompt(ENTRY, ROSTER, narrator="SUBARU",
                            variant="explicit_hint")
        self.assertIn("SUBARU", both)
        self.assertTrue(both.endswith(EXPLICIT_HINT))

    def test_the_variant_list_is_what_the_cli_offers(self):
        """Pinned exactly, so adding an arm is a decision and not a slip.

        It caught `shuffled_roster` being added on 2026-08-21, which is what it
        is for: an arm that appears without anyone noticing is an arm nobody
        wrote a hypothesis for.
        """
        self.assertEqual(("control", "explicit_hint", "shuffled_roster",
                          "inner_narration", "speaker_not_addressee",
                          # 2026-09-14, GOALS 1.2: the usual-suspect prior. The
                          # hypothesis was that naming the prior recovers what
                          # removing the leads recovers; it did on one book of three.
                          "minor_speaker_hint"),
                         PROMPT_VARIANTS)

    def test_minor_speaker_hint_is_appended_to_the_control_prompt(self):
        from experiments.two_stage_attribution import MINOR_SPEAKER_HINT
        control = build_prompt(ENTRY, ROSTER, variant="control")
        hinted = build_prompt(ENTRY, ROSTER, variant="minor_speaker_hint")
        self.assertEqual(control + MINOR_SPEAKER_HINT, hinted)
        self.assertIn("main characters", MINOR_SPEAKER_HINT)
