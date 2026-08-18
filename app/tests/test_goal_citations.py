"""Goals must be joinable to the evidence they rest on.

goal_evidence_audit joins GOALS.md to the structural audit by filename, so a
goal that never writes a filename reads as unsupported however much work backs
it. 25 of 30 goals were in that state.

The scanner is the load-bearing part, and its first version could not see a
hyphen - which excludes almost every artifact named after a model or endpoint
(`pdnc_evidence__pilot__local-llamacpp.json`). Citations added to goal 1.3 were
invisible to it, and the audit kept reporting "cites none": a scanner that
cannot spell, reported as missing evidence.
"""
import glob
import os
import unittest

from experiments.goal_evidence_audit import parse_goals

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOALS = os.path.join(REPO, "GOALS.md")


class CitationScannerTest(unittest.TestCase):
    def _cited_by(self, number):
        for goal in parse_goals(GOALS):
            if goal["number"] == number:
                return goal["cited"]
        self.fail(f"goal {number} not found in GOALS.md")

    def test_a_hyphenated_artifact_name_is_seen(self):
        """The bug, pinned: model-named artifacts all contain hyphens."""
        cited = self._cited_by("1.3")
        self.assertIn("pdnc_evidence__pilot__local-llamacpp.json", cited)

    def test_goals_measured_this_session_cite_their_artifacts(self):
        # Not a style rule - these three are the goals whose evidence was
        # produced and read end-to-end, so they are the ones that can be
        # asserted without guessing.
        self.assertIn("respelling_e_row__ay_n1600.json", self._cited_by("5.5"))
        self.assertIn("gate_promote__crisp_mezzo_30s_f.json", self._cited_by("2.7"))

    def test_every_cited_artifact_actually_exists(self):
        """A citation to a missing file is worse than none: it reads as
        checkable and is not.

        Not every citation is an experiment artifact, which is why this looks
        in more than one place. `pronunciation.json` is the shipped lexicon at
        the repo root and `training_meta.json` sits inside each adapter's own
        directory - both are legitimate things for a goal to point at, and an
        earlier version of this test called them missing because it only knew
        about ab_test_runtime/experiments.
        """
        roots = [os.path.join(REPO, "ab_test_runtime", "experiments"), REPO,
                 os.path.join(REPO, "app", "fixtures")]
        missing = []
        for goal in parse_goals(GOALS):
            for name in goal["cited"]:
                if any(os.path.exists(os.path.join(r, name)) for r in roots):
                    continue
                # Anywhere under the runtime tree counts: per-adapter files
                # like training_meta.json exist many times over.
                found = glob.glob(os.path.join(REPO, "ab_test_runtime", "**", name),
                                  recursive=True)
                if not found:
                    missing.append(f"{goal['number']} -> {name}")
        self.assertEqual([], missing, "cited artifacts that are nowhere on disk")
