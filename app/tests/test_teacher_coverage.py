"""Nothing is lost silently between routing and the teacher corpus.

The invariant: routed == teacher rows + recorded failures. It fails today by
195 rows across four books, 131 of them from reborn02 alone, and every one is
a genuine disagreement whose loss nothing records.
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.teacher_coverage import (  # noqa: E402
    CoverageError, assert_coverage)


def build(root, routed, rows, failures=None):
    """Write a routing artifact, a corpus, and optionally a failures sidecar."""
    rd = os.path.join(root, "routed"); os.makedirs(rd, exist_ok=True)
    for book, indices in routed.items():
        with open(os.path.join(rd, f"routed__{book}.json"), "w", encoding="utf-8") as fh:
            json.dump({"book": book, "a": {}, "b": {}, "routed": indices}, fh)
    for book, n in (failures or {}).items():
        with open(os.path.join(rd, f"routed__{book}.failures.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"failures": [{"segment_index": i, "reason": "no teacher response"}
                                    for i in range(n)]}, fh)
    corpus = os.path.join(root, "train.jsonl")
    with open(corpus, "w", encoding="utf-8") as fh:
        for book, n in rows.items():
            for i in range(n):
                fh.write(json.dumps({"book": book, "segment_index": i}) + "\n")
    return rd, [corpus]


class TeacherCoverageTest(unittest.TestCase):

    def test_a_complete_corpus_passes(self):
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": ["1", "2", "3"]}, {"a": 3})
            report = assert_coverage(rd, paths)
        self.assertEqual(0, report[0]["unexplained"])

    def test_a_silent_loss_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": ["1", "2", "3"]}, {"a": 1})
            with self.assertRaises(CoverageError) as caught:
                assert_coverage(rd, paths)
        self.assertIn("2 routed rows are missing", str(caught.exception))

    def test_recording_the_loss_satisfies_the_invariant(self):
        """The requirement is not that nothing is lost. It is that nothing is
        lost SILENTLY."""
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": ["1", "2", "3"]}, {"a": 1}, failures={"a": 2})
            report = assert_coverage(rd, paths)
        self.assertEqual(0, report[0]["unexplained"])
        self.assertEqual(2, report[0]["recorded_failures"])

    def test_over_recording_does_not_hide_a_further_loss(self):
        """Recorded failures cannot be used to paper over rows beyond them."""
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": [str(i) for i in range(10)]},
                              {"a": 4}, failures={"a": 3})
            with self.assertRaises(CoverageError):
                assert_coverage(rd, paths)

    def test_sources_that_were_never_routed_are_not_a_failure(self):
        """A mixture legitimately carries PDNC replay and older light novels;
        calling those a coverage gap would fire on every real corpus."""
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": ["1", "2"]}, {"a": 2, "pdnc_extra": 500})
            report = assert_coverage(rd, paths)
        self.assertEqual(["a"], [r["book"] for r in report])

    def test_allow_unexplained_reports_but_does_not_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            rd, paths = build(d, {"a": ["1", "2", "3"]}, {"a": 1})
            report = assert_coverage(rd, paths, allow_unexplained=True)
        self.assertEqual(2, report[0]["unexplained"],
                         "the gap must still be reported, not silenced")


if __name__ == "__main__":
    unittest.main()
