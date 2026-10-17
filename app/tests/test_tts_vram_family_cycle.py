"""The #1217 VRAM diagnostic's verdict must flag real retention and nothing else."""
import gc
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
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


class OwnerRegistryTests(unittest.TestCase):
    class Owner:
        pass

    def test_reused_id_replaces_dead_slot_and_detects_later_orphan(self):
        old = self.Owner()
        reused_id = id(old)
        old_ref = weakref.ref(old)
        del old
        gc.collect()
        self.assertIsNone(old_ref())
        released = {reused_id: ("model:custom", old_ref)}
        new = self.Owner()
        # IDs can be reused after collection; force that legal sequence without
        # depending on this interpreter's allocation/free-list behavior.
        with patch.object(cycle, "id", return_value=reused_id, create=True), \
                patch.object(cycle, "get_owned", return_value={"model:clone": [new]}):
            owned_ids, recorded = cycle.get_recorded_owners(None, released)
        self.assertEqual({reused_id}, owned_ids)
        self.assertIs(recorded[reused_id][1](), new)
        self.assertEqual("model:clone", recorded[reused_id][0])
        self.assertIs(released[reused_id][1], old_ref)

        with patch.object(cycle, "get_owned", return_value={}):
            owned_ids, recorded = cycle.get_recorded_owners(None, recorded)
        orphans = [label for key, (label, ref) in recorded.items()
                   if ref() is not None and key not in owned_ids]
        self.assertEqual(["model:clone"], orphans)
        new_ref = weakref.ref(new)
        del new
        gc.collect()
        self.assertIsNone(new_ref(), "the registry must not retain an owner")

    def test_fresh_and_still_live_slots_keep_weak_identity(self):
        owner = self.Owner()
        with patch.object(cycle, "get_owned", return_value={"model:custom": [owner]}):
            owned_ids, recorded = cycle.get_recorded_owners(None, {})
            owned_again, recorded_again = cycle.get_recorded_owners(None, recorded)
        self.assertEqual({id(owner)}, owned_ids)
        self.assertEqual(owned_ids, owned_again)
        self.assertIs(recorded_again[id(owner)], recorded[id(owner)])
        self.assertIs(recorded[id(owner)][1](), owner)
        with patch.object(cycle, "get_owned", return_value={}):
            no_longer_owned, tracked = cycle.get_recorded_owners(None, recorded_again)
        self.assertEqual(set(), no_longer_owned)
        self.assertIs(tracked[id(owner)][1](), owner)
        ref = weakref.ref(owner)
        del owner
        gc.collect()
        self.assertIsNone(ref())


if __name__ == "__main__":
    unittest.main()
