"""The listening arms must differ before a person is asked to rate them."""
import unittest

import os
import tempfile

from experiments.instruct_listening import (arm_requests, get_character_instruction,
                                            identical_arms)


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


class ArmRequestTests(unittest.TestCase):
    """Goal 7.1, 2026-08-22: the per_char arm rendered the same file as none,
    because the engine appends the entry's character_style to every request."""
    ENTRY = {"type": "lora", "adapter_path": "x", "character_style": "Dry.",
             "default_style": "Warm.", "seed": "7"}

    def test_every_arm_strips_the_styles_the_engine_would_re_add(self):
        for arm, (_, entry) in arm_requests(self.ENTRY, "Dry.", "Shout it.").items():
            self.assertNotIn("character_style", entry, arm)
            self.assertNotIn("default_style", entry, arm)
            self.assertEqual("x", entry["adapter_path"])

    def test_the_three_arms_send_three_different_instructions(self):
        req = arm_requests(self.ENTRY, "Dry.", "Shout it.")
        self.assertEqual("", req["none"][0])
        self.assertEqual("Dry.", req["per_char"][0])
        self.assertEqual("Shout it.", req["per_line"][0])
        self.assertEqual(3, len({i for i, _ in req.values()}))

    def test_identical_renders_are_named_and_distinct_ones_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = (os.path.join(tmp, f"{n}.wav") for n in "abc")
            for path, data in ((a, b"RIFF1"), (b, b"RIFF1"), (c, b"RIFF2")):
                with open(path, "wb") as fh:
                    fh.write(data)
            self.assertEqual(["none", "per_char"],
                             identical_arms({"none": a, "per_char": b, "per_line": c}))
            self.assertEqual([], identical_arms({"none": a, "per_line": c}))


if __name__ == "__main__":
    unittest.main()
