"""The shipped attribution prompt carries the usual-suspect rule (GOALS 1.2,
2026-09-14): measured never negative on seven books, +3.4 on the one book
whose bias it reaches. Pinned so a prompt edit cannot drop it unnoticed."""
import unittest

from default_prompts import load_attribute_prompts


class MinorSpeakerRule(unittest.TestCase):
    def test_rule_four_is_in_the_system_prompt_and_not_the_user_prompt(self):
        system, user = load_attribute_prompts()
        self.assertIn("Do not default to the story's main characters", system)
        self.assertNotIn("main characters", user)
        # it is a numbered rule after the UNKNOWN rule, not a stray sentence
        self.assertLess(system.index('use "UNKNOWN"'), system.index("4. Do not default"))


if __name__ == "__main__":
    unittest.main()
