"""A run that matched nothing must not report success.

On 2026-08-19 the separator_pauses stage wrote

    {"status": "complete", "candidates_considered": 7607,
     "results": [], "summary": {}}

in under a second, and the chain logged `OK separator_pauses (0s)`. Two causes,
both fixed here:

1. A row requires every arm to have a clip - correct, since a partial row would
   bias whichever arm is missing - but the arm set was the three BUILT-IN arms
   plus the requested ones. Asking for none/space/dot silently also demanded
   plain, eh and ay, so the six-way intersection was empty.
2. Zero matches was written out as a complete artifact rather than raised.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import experiments.measure_pauses as measure_pauses  # noqa: E402


class ArmSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = os.path.join(self.tmp.name, "ab_test_runtime")
        os.makedirs(self.runtime)
        patcher = mock.patch.object(measure_pauses, "REPO", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        # internal_pauses is exercised by test_pause_scoring on real audio;
        # here the question is which files get paired, so it is stubbed.
        stub = mock.patch.object(measure_pauses, "internal_pauses",
                                 side_effect=lambda p, **k: (1, 0.3))
        stub.start()
        self.addCleanup(stub.stop)

    def clips(self, directory, suffix, terms):
        path = os.path.join(self.runtime, directory)
        os.makedirs(path, exist_ok=True)
        for term in terms:
            open(os.path.join(path, term + suffix), "wb").close()

    def run_main(self, argv):
        out = os.path.join(self.tmp.name, "out.json")
        with mock.patch.object(sys, "argv", ["measure_pauses.py",
                                             "--out", out] + argv):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                measure_pauses.main()
        with open(out, encoding="utf-8") as handle:
            return json.load(handle), buffer.getvalue()

    def test_requested_arms_do_not_drag_in_the_built_in_ones(self):
        """plain + the two asked for. `eh` and `ay` have no clips here, and
        including them would empty the intersection - which is exactly what
        happened in production."""
        terms = ["alpha", "beta", "gamma"]
        self.clips("respelling_measure", "_plain.wav", terms)
        self.clips("sep_none", "_respelled.wav", terms)
        self.clips("sep_dot", "_respelled.wav", terms)
        doc, _ = self.run_main(["--arm", "none=sep_none",
                                "--arm", "dot=sep_dot"])
        self.assertEqual(3, len(doc["results"]))
        row = doc["results"][0]
        self.assertEqual({"term", "plain", "none", "dot"}, set(row))
        self.assertNotIn("eh", row)
        self.assertNotIn("ay", row)

    def test_zero_matches_raises_instead_of_writing_complete(self):
        """The production failure: one requested arm has no clips at all."""
        self.clips("respelling_measure", "_plain.wav", ["alpha", "beta"])
        self.clips("sep_none", "_respelled.wav", ["alpha", "beta"])
        os.makedirs(os.path.join(self.runtime, "sep_space"), exist_ok=True)
        with self.assertRaises(SystemExit) as caught:
            self.run_main(["--arm", "none=sep_none", "--arm", "space=sep_space"])
        message = str(caught.exception)
        self.assertIn("measured nothing", message)
        self.assertIn("space", message, "the empty arm must be named - it is "
                                        "always the actual cause")
        self.assertIn("per-arm clip counts", message)

    def test_a_term_missing_from_one_arm_is_dropped_not_half_counted(self):
        """The every-arm-or-none rule is deliberate and must survive."""
        self.clips("respelling_measure", "_plain.wav", ["alpha", "beta"])
        self.clips("sep_none", "_respelled.wav", ["alpha"])
        doc, _ = self.run_main(["--arm", "none=sep_none"])
        self.assertEqual(1, len(doc["results"]))
        self.assertEqual("alpha", doc["results"][0]["term"])

    def test_with_no_arm_flags_the_built_in_arms_still_apply(self):
        """Callers that pass no --arm must behave exactly as before."""
        self.clips("respelling_measure", "_plain.wav", ["alpha"])
        self.clips("respelling_measure", "_respelled.wav", ["alpha"])
        self.clips("respelling_e_row_ay", "_respelled.wav", ["alpha"])
        doc, _ = self.run_main([])
        self.assertEqual(1, len(doc["results"]))
        self.assertEqual({"term", "plain", "eh", "ay"}, set(doc["results"][0]))


if __name__ == "__main__":
    unittest.main()
