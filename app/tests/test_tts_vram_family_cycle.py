"""The #1217 VRAM diagnostic's verdict must flag real retention and nothing else."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "tts_vram_family_cycle",
    Path(__file__).parent.parent.parent / "tools" / "diagnostics" / "tts_vram_family_cycle.py")
cycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cycle)

MIB = 1024 * 1024
GIB = 1024 * MIB


def make_samples(growth=None, orphans=None, reserved=0, warmup=None, cycles=(1, 2, 3)):
    """Cycles of the five steps at fixed allocations, optionally with one step
    growing per cycle, one step's one-time warm-up from cycle 2 on, or a
    released object reported alive."""
    allocated = {"custom": 4 * GIB, "clone": 4 * GIB + 3 * MIB, "design": 4 * GIB,
                 "lora_a": 4 * GIB + MIB, "lora_b": 4 * GIB + MIB}
    samples = []
    for number in cycles:
        for step in cycle.STEPS:
            extra = growth[1] * (number - 1) if growth and growth[0] == step else 0
            extra += warmup[1] if warmup and warmup[0] == step and number > 1 else 0
            samples.append({"cycle": number, "step": step,
                            "allocated": allocated[step] + extra,
                            "reserved": reserved or allocated[step],
                            "orphans": list(orphans or []) if number == 2 and step == "design" else []})
    return samples


class CycleVerdictTests(unittest.TestCase):
    def test_flat_cycles_and_restored_baseline_are_clean(self):
        verdict = cycle.get_cycle_verdict(make_samples(), 0, 0)
        self.assertEqual(verdict["verdict"], "CLEAN")
        self.assertEqual(set(verdict["step_deltas"].values()), {0})

    def test_one_step_growing_each_cycle_is_a_leak_at_that_step(self):
        verdict = cycle.get_cycle_verdict(make_samples(growth=("lora_b", 2 * MIB)), 0, 0)
        self.assertEqual(verdict["verdict"], "LEAK")
        self.assertEqual(verdict["step_deltas"]["lora_b"], 2 * MIB)
        self.assertEqual(len(verdict["reasons"]), 1)
        self.assertIn("lora_b", verdict["reasons"][0])

    def test_one_time_first_cycle_allocation_is_warmup_not_a_leak(self):
        # Measured shape (2026-10-17): CustomVoice +2 MiB from cycle 1 to 2, flat after.
        verdict = cycle.get_cycle_verdict(make_samples(warmup=("custom", 2 * MIB)), 0, 0)
        self.assertEqual(verdict["verdict"], "CLEAN")
        self.assertEqual(verdict["warmup_deltas"]["custom"], 2 * MIB)
        self.assertEqual(verdict["step_deltas"]["custom"], 0)

    def test_warmup_followed_by_growth_is_still_a_leak(self):
        verdict = cycle.get_cycle_verdict(
            make_samples(warmup=("clone", 8 * MIB), growth=("clone", 2 * MIB)), 0, 0)
        self.assertEqual(verdict["verdict"], "LEAK")
        self.assertEqual(verdict["step_deltas"]["clone"], 2 * MIB)

    def test_two_cycles_cannot_separate_warmup_from_growth(self):
        with self.assertRaises(ValueError):
            cycle.get_cycle_verdict(make_samples(cycles=(1, 2)), 0, 0)

    def test_growth_inside_tolerance_is_not_a_leak(self):
        verdict = cycle.get_cycle_verdict(make_samples(growth=("clone", 256 * 1024)), 0, 0)
        self.assertEqual(verdict["verdict"], "CLEAN")

    def test_allocation_left_after_dropping_the_engine_is_a_leak(self):
        verdict = cycle.get_cycle_verdict(make_samples(), 76 * MIB, 76 * MIB + 64 * MIB)
        self.assertEqual(verdict["verdict"], "LEAK")
        self.assertEqual(verdict["end_delta"], 64 * MIB)

    def test_a_released_object_still_alive_is_a_leak_even_with_flat_bytes(self):
        verdict = cycle.get_cycle_verdict(make_samples(orphans=["model:clone"]), 0, 0)
        self.assertEqual(verdict["verdict"], "LEAK")
        self.assertIn("cycle 2 design", verdict["reasons"][0])

    def test_high_reserved_with_flat_allocated_is_clean(self):
        verdict = cycle.get_cycle_verdict(make_samples(reserved=12 * GIB), 0, 0)
        self.assertEqual(verdict["verdict"], "CLEAN")


if __name__ == "__main__":
    unittest.main()
