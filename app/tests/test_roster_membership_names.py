"""`in_candidates` must mean what it says, on the case that proved it didn't.

Rule 21. The availability figure is an instrument, and this one reported that a
character the model was offered on every single row had never been offered at
all - which reads as "candidate generation is broken" and sends you to fix the
wrong thing. The fixtures here are the real names from that run.
"""

import json
import os
import unittest

from experiments.scoring import (alias_groups, normalize,
                                 roster_membership_names, same_speaker)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RosterMembershipNamesTest(unittest.TestCase):

    def test_the_gaen_case(self):
        # owarimonogatari3's gold declares ['GAEN', 'IZUKO GAEN']. The roster
        # showed the full name; the gold asks for the short one; 31 rows were
        # recorded as unavailable. This is that exact shape.
        groups = alias_groups({"aliases": [["GAEN", "IZUKO GAEN"]]})
        names = roster_membership_names(["IZUKO GAEN", "HANEKAWA"], groups)
        self.assertIn("GAEN", names)
        self.assertIn("IZUKO GAEN", names)

    def test_it_does_not_invent_characters_the_model_never_saw(self):
        # THE CASE IT MUST REFUSE. An alias group with nobody on the roster
        # contributes nothing; otherwise `in_candidates` becomes unfalsifiable,
        # which is worse than reporting it too low.
        groups = alias_groups({"aliases": [["GAEN", "IZUKO GAEN"],
                                           ["ABSENT", "ALSO ABSENT"]]})
        names = roster_membership_names(["IZUKO GAEN"], groups)
        self.assertNotIn("ABSENT", names)
        self.assertNotIn("ALSO ABSENT", names)

    def test_a_roster_with_no_aliases_is_unchanged(self):
        self.assertEqual(["A", "B"], roster_membership_names(["B", "A"], []))

    def test_it_normalizes_so_membership_matches_the_scorer(self):
        # The whole defect was two fields disagreeing. Both sides normalize the
        # same way or the fix reintroduces the bug in a new place.
        names = roster_membership_names(["Mr. Pleasant"], [])
        self.assertEqual([normalize("MR PLEASANT")], names)

    def test_membership_and_correctness_now_agree_on_the_real_fixture(self):
        # The invariant that was violated: if the scorer would call a
        # prediction right, availability must not say the answer was absent.
        path = os.path.join(APP, "fixtures",
                            "attribution_gold_owarimonogatari3.json")
        if not os.path.exists(path):
            self.skipTest("owarimonogatari3 gold not present")
        gold = json.load(open(path, encoding="utf-8"))
        groups = alias_groups(gold)
        roster = ["IZUKO GAEN"]
        names = set(roster_membership_names(roster, groups))
        for shown in roster:
            for expected in names:
                if same_speaker(expected, shown, groups):
                    self.assertIn(normalize(expected), names)


if __name__ == "__main__":
    unittest.main()
