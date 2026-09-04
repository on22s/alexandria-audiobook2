"""The fidelity control arm must differ from the adapter arm in the voice ALONE.

The library-wide f0_median 1.07 and f0_spread 1.15 are ratios against each
narrator with no null to read them against: some of that is the adapter and
some is "any synthetic voice vs this human". The control answers that only if
it really bypasses the LoRA, so these tests check the routing decision itself
rather than that a function returned something.
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments",
                      "library_voice_fidelity_resume_20260831.py")


def _load():
    spec = importlib.util.spec_from_file_location("libfid", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VoiceEntry(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_default_arm_loads_the_adapter(self):
        e = self.mod.voice_entry("", "/models", "warm_tenor_20s_m", 20260925)
        self.assertEqual("lora", e["type"])
        self.assertIn("warm_tenor_20s_m", e["adapter_path"])

    def test_control_arm_carries_no_adapter_path(self):
        """The failure that would make the control worthless: still loading
        the LoRA and reporting it as a null."""
        e = self.mod.voice_entry("Ryan", "/models", "warm_tenor_20s_m", 20260925)
        self.assertNotIn("adapter_path", e)
        self.assertNotIn("warm_tenor_20s_m", repr(e))
        self.assertEqual("Ryan", e["voice"])

    def test_control_arm_routes_away_from_the_lora_generator(self):
        """voice_category() is what tts.render branches on, so assert on it -
        an entry that merely lacks adapter_path but still says type=lora would
        be dispatched to the LoRA path anyway."""
        import sys
        sys.path.insert(0, os.path.join(REPO, "app"))
        from tts import voice_category
        self.assertEqual("lora", voice_category(
            self.mod.voice_entry("", "/m", "a", 1)))
        self.assertEqual("custom", voice_category(
            self.mod.voice_entry("Ryan", "/m", "a", 1)))

    def test_both_arms_use_the_same_seed(self):
        """Different seeds would confound the comparison: the LoRA path
        silently ignored its seed once already, which made every A/B on it
        uncontrolled."""
        a = self.mod.voice_entry("", "/m", "a", 4242)
        b = self.mod.voice_entry("Ryan", "/m", "a", 4242)
        self.assertEqual("4242", a["seed"])
        self.assertEqual(a["seed"], b["seed"])


if __name__ == "__main__":
    unittest.main()
