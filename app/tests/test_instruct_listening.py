"""The listening arms must differ before a person is asked to rate them."""
import unittest

from experiments.instruct_listening import get_character_instruction


class CharacterInstructionTests(unittest.TestCase):
    def test_configured_character_style_wins(self):
        self.assertEqual(
            "Dry and restrained.",
            get_character_instruction(
                {"character_style": "  Dry and restrained.  "}, "NARRATOR"),
        )

    def test_missing_style_uses_the_shared_character_constant(self):
        self.assertEqual(
            "Clear, measured narration.",
            get_character_instruction({}, "NARRATOR"),
        )

    def test_unknown_speaker_still_gets_a_nonempty_arm(self):
        self.assertEqual(
            "Natural conversational delivery.",
            get_character_instruction({}, "KENJI"),
        )


if __name__ == "__main__":
    unittest.main()
