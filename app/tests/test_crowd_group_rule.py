"""The #653 crowd-rule experiment: one prompt rule changed, verdict computed."""
import unittest

from attribution_prompt_variants import MICHEL2_SYSTEM
from experiments import crowd_group_rule as cg


def _chapter(base_correct, cand_correct):
    run = lambda c: {"crowd": {"n": 7, "correct": c}}
    return {"cast": [run(base_correct)] * 3, "groups": [run(cand_correct)] * 3}


def _pdnc(cand_named=93.0, cand_unnamed=72.0, repeat_named=92.6):
    return {"cast": {"named_cast_alias_pct": 93.0, "descriptive_cast_alias_pct": 72.0},
            "groups": {"named_cast_alias_pct": cand_named, "descriptive_cast_alias_pct": cand_unnamed},
            "cast_repeat": {"named_cast_alias_pct": repeat_named, "descriptive_cast_alias_pct": 71.0}}


class CrowdRuleTest(unittest.TestCase):
    def test_only_rule_seven_changes(self):
        new = cg.get_group_rule_system()
        changed = [(a, b) for a, b in zip(MICHEL2_SYSTEM.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(1, len(changed))
        self.assertTrue(changed[0][0].startswith("7.") and changed[0][1].startswith("7."))

    def test_gate_passes_on_more_crowd_lines_and_no_loss(self):
        self.assertTrue(cg.get_gate(_chapter(0, 5), _pdnc(), "cast", "groups", "cast_repeat")["passes"])

    def test_gate_fails_without_a_crowd_gain(self):
        self.assertFalse(cg.get_gate(_chapter(0, 0), _pdnc(), "cast", "groups", "cast_repeat")["passes"])

    def test_named_drop_is_bounded_by_spread_capped_at_half_a_point(self):
        def named_pass(cand, repeat):
            return cg.get_gate(_chapter(0, 5), _pdnc(cand_named=cand, repeat_named=repeat),
                               "cast", "groups", "cast_repeat")["named_pass"]
        self.assertFalse(named_pass(92.4, 90.0))   # spread 3.0 capped to 0.5; drop 0.6
        self.assertTrue(named_pass(92.6, 92.0))    # spread 1.0 capped to 0.5; drop 0.4
        self.assertFalse(named_pass(92.8, 92.9))   # spread 0.1 is tighter; drop 0.2

    def test_unnamed_individuals_may_not_drop_beyond_the_bound(self):
        self.assertFalse(cg.get_gate(_chapter(0, 5), _pdnc(cand_unnamed=70.0),
                                     "cast", "groups", "cast_repeat")["passes"])


if __name__ == "__main__":
    unittest.main()
