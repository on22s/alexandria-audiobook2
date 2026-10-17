"""The #1217 VRAM diagnostic's verdict must flag real retention and nothing else."""
import importlib.util
from pathlib import Path
import unittest
import weakref

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



class Model:
    """Stands in for a loaded model: weakref-able, identity only."""


class FakeEngine:
    def __init__(self, **models):
        for family in cycle.FAMILIES:
            setattr(self, f"_local_{family}_model", models.get(family))
        self._clone_prompt_cache = {}
        self._lora_prompt_cache = {}


def get_dead_ref():
    gone = Model()
    ref = weakref.ref(gone)
    del gone
    return ref


class RecordedOwnerTests(unittest.TestCase):
    """#1292: Python may give a new owner the id of a dead one."""

    def test_a_dead_entry_under_a_reused_id_is_replaced_by_the_new_owner(self):
        model = Model()
        stale = {id(model): ("model:clone", get_dead_ref())}  # the id reuse, made explicit
        owned_ids, recorded = cycle.get_recorded_owners(FakeEngine(custom=model), stale)
        self.assertIn(id(model), owned_ids)
        label, ref = recorded[id(model)]
        self.assertIs(ref(), model)
        self.assertEqual(label, "model:custom")

    def test_the_new_owner_is_reported_once_something_outside_keeps_it_alive(self):
        model = Model()
        stale = {id(model): ("model:clone", get_dead_ref())}
        _, recorded = cycle.get_recorded_owners(FakeEngine(custom=model), stale)
        # Next step: the engine evicted it, but this test still holds `model`.
        owned_ids, recorded = cycle.get_recorded_owners(FakeEngine(), recorded)
        self.assertEqual(cycle.get_orphans(recorded, owned_ids), ["model:custom"])

    def test_an_owner_still_held_by_the_engine_is_not_an_orphan(self):
        model = Model()
        _, recorded = cycle.get_recorded_owners(FakeEngine(lora=model), {})
        owned_ids, recorded = cycle.get_recorded_owners(FakeEngine(lora=model), recorded)
        self.assertEqual(cycle.get_orphans(recorded, owned_ids), [])
        self.assertEqual(recorded[id(model)][0], "model:lora")

    def test_the_callers_registry_is_not_changed(self):
        stale = {1: ("model:clone", get_dead_ref())}
        before = dict(stale)
        cycle.get_recorded_owners(FakeEngine(design=Model()), stale)
        self.assertEqual(stale, before)

if __name__ == "__main__":
    unittest.main()
