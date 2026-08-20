"""An artifact may shrink only if it says why.

dataset_ref_audit.json held 101 measured rows, was rewritten by a replay with
`results: []` and no explanation, and that empty version sat on main for two
days. The only surviving copy of the rows was on two old feature branches.

Two other artifacts were emptied by the same replay and are FINE - they carry
`failures: [{"rc": 1, ...}]`, a run saying it could not reproduce the result.
Both cases are fixtures here, because a guard that cannot tell them apart
would either miss the loss or block the honest report.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.check_artifact_shrinkage import (  # noqa: E402
    explained, row_count, scan)


class RowCountTest(unittest.TestCase):
    def test_both_list_contracts_are_counted(self):
        self.assertEqual(2, row_count({"rows": [1, 2]}))
        self.assertEqual(3, row_count({"results": [1, 2, 3]}))

    def test_a_document_with_no_row_list_is_not_judged(self):
        """A comparison artifact keeps its data under its own names, and
        shrinking is not defined for it. None, not 0 - 0 would claim it was
        measured and found empty."""
        self.assertIsNone(row_count({"pilots": [{"a": 1}]}))
        self.assertIsNone(row_count([1, 2, 3]))


class ExplainedTest(unittest.TestCase):
    def test_a_recorded_failure_counts_as_an_explanation(self):
        self.assertTrue(explained({"results": [],
                                   "failures": [{"book": "x", "rc": 1}]}))

    def test_an_empty_failures_list_explains_nothing(self):
        """`failures: []` beside `results: []` says the run thinks it
        succeeded, which is the silent case wearing the right shape."""
        self.assertFalse(explained({"results": [], "failures": []}))

    def test_silence_is_not_an_explanation(self):
        self.assertFalse(explained({"results": []}))


class ScanTest(unittest.TestCase):
    """Drives the real git plumbing against a throwaway repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.experiments = os.path.join(self.repo, "ab_test_runtime",
                                        "experiments")
        os.makedirs(self.experiments)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, check=True)

    def write(self, name, payload):
        with open(os.path.join(self.experiments, name), "w",
                  encoding="utf-8") as handle:
            json.dump(payload, handle)

    def commit(self, message="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def test_a_silent_emptying_is_reported(self):
        self.write("audit.json", {"results": [{"i": n} for n in range(101)]})
        self.commit()
        self.write("audit.json", {"results": []})
        silent, ok = scan(self.repo, "HEAD")
        self.assertEqual(1, len(silent))
        self.assertEqual(("audit.json", 101, 0),
                         (silent[0]["artifact"], silent[0]["was"],
                          silent[0]["now"]))
        self.assertEqual([], ok)

    def test_an_explained_emptying_is_allowed(self):
        self.write("three_pass.json", {"results": [{"i": 1}]})
        self.commit()
        self.write("three_pass.json",
                   {"results": [], "failures": [{"book": "x", "rc": 1}]})
        silent, ok = scan(self.repo, "HEAD")
        self.assertEqual([], silent)
        self.assertEqual(1, len(ok))
        self.assertTrue(ok[0]["explained"])

    def test_growth_is_not_a_shrink(self):
        self.write("audit.json", {"results": [{"i": 1}]})
        self.commit()
        self.write("audit.json", {"results": [{"i": 1}, {"i": 2}]})
        self.assertEqual(([], []), scan(self.repo, "HEAD"))

    def test_an_unchanged_artifact_is_not_reported(self):
        self.write("audit.json", {"results": [{"i": 1}]})
        self.commit()
        self.assertEqual(([], []), scan(self.repo, "HEAD"))

    def test_a_brand_new_artifact_has_nothing_to_shrink_from(self):
        self.write("first.json", {"results": [{"i": 1}]})
        self.commit()
        self.write("second.json", {"results": []})
        silent, ok = scan(self.repo, "HEAD")
        self.assertEqual([], silent, "an artifact with no previous version "
                                     "cannot have lost rows")

    def test_a_shape_without_rows_is_skipped_rather_than_guessed_at(self):
        self.write("compare.json", {"pilots": [{"a": 1}, {"b": 2}]})
        self.commit()
        self.write("compare.json", {"pilots": []})
        self.assertEqual(([], []), scan(self.repo, "HEAD"))


if __name__ == "__main__":
    unittest.main()
