"""The speaker-attestation gate must be off for every PDNC fixture, in every copy.

PDNC names characters the corpus way ("A WAITER", "NANNY", "MAMA"), which the
text writes in lower case, so the production gate rejects correct answers and
burns every retry. The harness switched it off only when a fixture carried
`roster_additions.attest_in_source` -- a block main's committed fixtures lack.
On 2026-09-27 the 28-book goal-1.3 run from a clean checkout ran with the gate
ON (the log shows NANNY and MAMA rejected) while every nine-novel cell on the
boxes ran with it OFF: two instruments under one name. Same class of drift as
#670's roster fallback.
"""
import glob
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, REPO)
from experiments.lora_serving_eval import is_attestation_gate_off  # noqa: E402
from tools.audit.audit_legacy_attribution import read_gold_fixture  # noqa: E402

FIXTURES = os.path.join(REPO, "app", "fixtures")


def committed(pattern):
    return sorted(glob.glob(os.path.join(FIXTURES, pattern)))


def read_committed(path):
    """Load a committed gold fixture through the tracked-link-aware reader.

    Four gold fixtures are committed as mode-120000 links. With
    core.symlinks=false Git materialises them as files holding the target
    path, which is not JSON. The shared reader resolves both forms and
    refuses a pointer that is not the tracked one.
    """
    rel = os.path.relpath(path, REPO).replace(os.sep, "/")
    return read_gold_fixture(path, rel)[1]


class AttestationGateFallback(unittest.TestCase):

    def test_every_committed_pdnc_fixture_turns_the_gate_off(self):
        paths = committed("attribution_gold_pdnc_*.json")
        self.assertGreaterEqual(len(paths), 9, "expected at least the nine evaluation fixtures "
                                "(generated ones are gitignored, #667)")
        on = [os.path.basename(p) for p in paths
              if not is_attestation_gate_off(read_committed(p))]
        self.assertEqual([], on, "these PDNC fixtures would run with the gate ON")

    def test_the_old_rule_fails_them(self):
        """Pin the discrimination: the pre-fix expression left the gate on."""
        paths = committed("attribution_gold_pdnc_*.json")
        old_rule_off = [p for p in paths
                        if read_committed(p).get("roster_additions", {}).get("attest_in_source")]
        self.assertLess(len(old_rule_off), len(paths),
                        "every committed fixture now carries the block; this test "
                        "has stopped discriminating")

    def test_non_pdnc_fixtures_keep_the_gate(self):
        others = [p for p in committed("attribution_gold_*.json") if "_pdnc_" not in p]
        self.assertTrue(others)
        off = [os.path.basename(p) for p in others
               if is_attestation_gate_off(read_committed(p))]
        self.assertEqual([], off, "the gate must stay on for non-PDNC gold")

    def test_an_explicit_block_still_decides(self):
        self.assertTrue(is_attestation_gate_off(
            {"source": "RiQuA", "roster_additions": {"attest_in_source": True}}))
        self.assertFalse(is_attestation_gate_off(
            {"source": "PDNC", "roster_additions": {"attest_in_source": False}}))
        self.assertTrue(is_attestation_gate_off({"source": "PDNC"}))
        self.assertTrue(is_attestation_gate_off({"source": "PDNC", "roster_additions": {}}))
        self.assertFalse(is_attestation_gate_off({"source": "RiQuA"}))


if __name__ == "__main__":
    unittest.main()
