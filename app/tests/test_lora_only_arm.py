"""--lora-only runs the adapter arm alone, and the artifact must say so.

Why it exists: Muse IQ2_XXS's base holds the JSON contract on 42 of 2,655
nine-novel rows (1.1%) and burns every retry doing it, ~500-700 s a window on an
A6000. A paired nine-novel cell would spend ~60 h re-measuring that known
collapse just to score the adapter next to it. The adapter arm alone takes a few
hours; the price is that the result is unpaired, which is what these tests hold
the artifact to saying.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.lora_serving_eval import get_eval_arms, get_eval_metadata  # noqa: E402
from experiments.audit_serving_metadata import audit_artifact  # noqa: E402
import experiments.lora_serving_eval as lse  # noqa: E402


class LoraOnlyArm(unittest.TestCase):

    def test_runs_only_the_adapter_at_scale_one(self):
        self.assertEqual((("lora", 1.0),), get_eval_arms(lora_only=True))

    def test_default_and_base_only_are_unchanged(self):
        self.assertEqual((("base", 0.0), ("lora", 1.0)), get_eval_arms())
        self.assertEqual((("base", None),), get_eval_arms(base_only=True))

    def test_both_modes_at_once_is_refused(self):
        with self.assertRaises(ValueError):
            get_eval_arms(base_only=True, lora_only=True)

    def test_the_cli_refuses_both_flags(self):
        argv = ["lora_serving_eval.py", "--books", "x", "--model", "m", "--tag", "t",
                "--base-only", "--lora-only"]
        with patch.object(sys, "argv", argv), patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                lse.main()

    def test_metadata_records_one_arm_and_says_it_is_unpaired(self):
        decoding, notes = get_eval_metadata(lora_only=True)
        self.assertEqual(["lora"], decoding["arms"])
        self.assertIn("nothing here is paired", notes)
        self.assertNotIn("Paired serving evaluation", notes)

    def test_the_serving_audit_does_not_flag_it_as_a_false_paired_claim(self):
        decoding, notes = get_eval_metadata(lora_only=True)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lora_serving_eval__x.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"meta": {"model": "m", "notes": notes, "decoding": decoding},
                           "rows": [{"arm": "lora", "id": "b:1", "correct": True,
                                     "predicted": "NAME"}]}, fh)
            result = audit_artifact(path)
        self.assertEqual(["lora"], result["actual_arms"])
        self.assertFalse(result["false_paired_claim"])


if __name__ == "__main__":
    unittest.main()
