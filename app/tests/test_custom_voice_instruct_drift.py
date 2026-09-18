"""The arm builder for custom_voice_instruct_drift: what each arm sends."""
import unittest

from experiments.custom_voice_instruct_drift import ANCHOR, instruct_for, strip_timbre


class ArmTests(unittest.TestCase):
    def test_no_timbre_removes_only_identity_words(self):
        self.assertEqual("Exhausted realization; low voice cracking slightly under stress.",
                         strip_timbre("Exhausted realization; breathy, low voice cracking slightly under stress."))
        self.assertEqual("Cold fury, barely contained, voice tight",
                         strip_timbre("Cold fury, barely contained, voice tight"))
        self.assertEqual("neutral", strip_timbre("Gravelly, breathy."))

    def test_each_arm_sends_what_it_says(self):
        written = "Gravelly narration; heavy effort."
        self.assertEqual(written, instruct_for("as_written", written))
        self.assertNotIn("Gravelly", instruct_for("no_timbre", written))
        self.assertTrue(instruct_for("anchored", written).startswith(ANCHOR))
        self.assertTrue(instruct_for("anchored", written).endswith(written))
        self.assertEqual(ANCHOR, instruct_for("anchor_only", written))
