"""Anchors are already long; the Chinese ceiling clears its arms by 0.0004.

Two things this file protects.

First, `ANCHOR_MIN_SECONDS = 7.0` and `build_anchor_side` concatenating to meet
it are what make every anchor on disk 8.8-9.5s. A plan to "lengthen the Chinese
anchor" is a plan to rebuild what exists, and the audit must keep saying so -
by measuring, not by asserting, so that a regression in the concatenation
flips the verdict instead of being hidden by it.

Second, "clears its arms" is a boolean hiding a magnitude:

    ljspeech (en)   ceiling 0.8328  best arm 0.7567   gap 0.0761
    kokoro   (ja)   ceiling 0.8355  best arm 0.7789   gap 0.0566
    aishell3 (zh)   ceiling 0.7655  best arm 0.7651   gap 0.0004

All three are True. One of them is a bound in name only, and the gap is what
says which.
"""
import json
import os
import tempfile
import unittest

from experiments.anchor_length_audit import audit, median


def artifact(rows, summary):
    handle = tempfile.NamedTemporaryFile("w", suffix="_score.json", delete=False)
    json.dump({"rows": rows, "summary": summary}, handle)
    handle.close()
    return handle.name


def row(anchor, test):
    return {"human_seconds": test,
            "human_vs_human": {"ecapa": 0.8, "anchor_seconds": anchor}}


class MedianTest(unittest.TestCase):
    def test_an_empty_list_is_none(self):
        self.assertIsNone(median([]))


class AuditTest(unittest.TestCase):
    def _audit(self, rows, summary):
        path = artifact(rows, summary)
        try:
            return audit(path)
        finally:
            os.unlink(path)

    def test_both_sides_are_reported_separately(self):
        """Anchor and test are different clips and only one of them varies."""
        out = self._audit([row(9.0, 3.0), row(10.0, 4.0)],
                          {"human_vs_human": {"ecapa": 0.77},
                           "clone": {"ecapa": 0.70}})
        self.assertEqual(9.5, out["anchor_seconds_median"])
        self.assertEqual(3.5, out["test_seconds_median"])

    def test_the_gap_is_reported_not_just_the_boolean(self):
        """A ceiling clearing by 0.0004 and by 0.08 must not read the same."""
        thin = self._audit([row(9.0, 3.0)],
                           {"human_vs_human": {"ecapa": 0.7655},
                            "clone": {"ecapa": 0.7651}})
        wide = self._audit([row(9.0, 7.0)],
                           {"human_vs_human": {"ecapa": 0.8328},
                            "clone": {"ecapa": 0.7567}})
        self.assertTrue(thin["ceiling_clears_every_arm"])
        self.assertTrue(wide["ceiling_clears_every_arm"])
        self.assertAlmostEqual(0.0004, thin["closest_arm_gap"], 4)
        self.assertAlmostEqual(0.0761, wide["closest_arm_gap"], 4)

    def test_a_ceiling_below_an_arm_is_flagged(self):
        out = self._audit([row(9.0, 3.0)],
                          {"human_vs_human": {"ecapa": 0.69},
                           "clone": {"ecapa": 0.765}})
        self.assertFalse(out["ceiling_clears_every_arm"])
        self.assertLess(out["closest_arm_gap"], 0)

    def test_the_shortest_anchor_is_kept_not_only_the_median(self):
        """One short anchor among many is what a median would hide."""
        out = self._audit([row(9.0, 3.0), row(9.0, 3.0), row(2.0, 3.0)],
                          {"human_vs_human": {"ecapa": 0.8},
                           "clone": {"ecapa": 0.7}})
        self.assertEqual(9.0, out["anchor_seconds_median"])
        self.assertEqual(2.0, out["anchor_seconds_min"])

    def test_an_artifact_with_no_anchor_seconds_is_skipped(self):
        self.assertIsNone(self._audit(
            [{"human_seconds": 3.0, "human_vs_human": {"ecapa": 0.8}}],
            {"human_vs_human": {"ecapa": 0.8}}))

    def test_a_list_shaped_file_is_skipped_rather_than_crashing(self):
        handle = tempfile.NamedTemporaryFile("w", suffix="_score.json", delete=False)
        json.dump([1, 2, 3], handle)
        handle.close()
        try:
            self.assertIsNone(audit(handle.name))
        finally:
            os.unlink(handle.name)
