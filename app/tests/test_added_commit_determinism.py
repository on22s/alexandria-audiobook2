"""The recorded provenance of an artifact must not depend on where you stand.

`results_index.csv` is regenerated in CI and compared against the committed
copy, so every column in it has to be a function of the tree's CONTENT. The
`added_commit` column was not: it came from `git log --diff-filter=A -1`, which
applies history simplification and follows a single parent through each merge.

A file that reached main through a squash-merge has two adding commits - the
branch's original, and main's squashed copy - and the simplified walk reports
whichever one its chosen path meets first. Measured 2026-08-22 on
pitch_quality_longref.json: the PR branch reported d24a5928, GitHub's merge ref
reported 6da598d1, same file, same bytes. #404 failed CI twice on that alone.

These tests build the topology rather than describing it, because the bug is a
property of the graph and nothing short of a real merge reproduces it.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments import replay_artifact


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def build_squash_topology(root):
    """A repo where one file has TWO adding commits, as a squash-merge leaves.

    -> (branch_tree, merge_tree), the two vantage points that disagreed.
    """
    origin = os.path.join(root, "origin")
    os.makedirs(os.path.join(origin, "ab_test_runtime", "experiments"))
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    base = os.path.join("ab_test_runtime", "experiments", "base.json")
    Path(origin, base).write_text("{}")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "base")

    artifact = os.path.join("ab_test_runtime", "experiments", "art.json")
    # The feature branch adds the artifact.
    git(origin, "checkout", "-q", "-b", "feature")
    Path(origin, artifact).write_text('{"rows": []}')
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "add artifact on branch")

    # Main gets the SAME file as a separate commit - what squash-merging does.
    git(origin, "checkout", "-q", "main")
    Path(origin, artifact).write_text('{"rows": []}')
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "add artifact squashed onto main")

    # THE TWO MERGES MUST BE BUILT ON SEPARATE BRANCHES, and with --no-ff.
    # A first version of this ran both from the same checkout: the second
    # merge fast-forwarded onto the first, so both "vantage points" were the
    # same commit and the topology could not possibly disagree. The test that
    # pins the old behaviour caught it - which is the only reason it is here.
    #
    # What differs between the two is PARENT ORDER, and that is precisely what
    # history simplification follows.

    # Vantage 1: the branch with main merged into it - what a contributor runs
    # ready.sh on.
    git(origin, "checkout", "-q", "-b", "vantage_branch", "feature")
    git(origin, "merge", "-q", "--no-ff", "main", "--no-edit", "-m", "merge main")
    branch_head = git(origin, "rev-parse", "HEAD")

    # Vantage 2: main with the branch merged into it - GitHub's
    # refs/pull/N/merge, the tree CI actually checks. Same content, parents
    # the other way round.
    git(origin, "checkout", "-q", "-b", "vantage_merge", "main")
    git(origin, "merge", "-q", "--no-ff", "feature", "--no-edit", "-m", "merge feature")
    merge_head = git(origin, "rev-parse", "HEAD")

    assert branch_head != merge_head, "the two vantage points collapsed into one"
    return origin, artifact, branch_head, merge_head


class AddedCommitDeterminismTests(unittest.TestCase):

    def _resolve_old(self, repo, head, path):
        """Retain the known-bad algorithm only as a fixture negative control."""
        git(repo, "checkout", "-q", head)
        out = git(repo, "log", "--diff-filter=A", "--format=%H %as", "-1", "--", path)
        return out.partition(" ")[0] if out else None

    def test_shipped_resolution_is_deterministic_on_discriminating_merge_topology(self):
        with tempfile.TemporaryDirectory() as root:
            repo, path, branch, merge = build_squash_topology(root)
            with self.subTest(check="shipped resolver"):
                with patch.object(replay_artifact, "REPO", repo):
                    git(repo, "checkout", "-q", branch)
                    a = replay_artifact.added_commit(path)
                    git(repo, "checkout", "-q", merge)
                    b = replay_artifact.added_commit(path)
                self.assertIsNotNone(a[0])
                self.assertIsNotNone(a[1])
                self.assertEqual(a, b,
                                 "CI merge and contributor branch must record identical provenance")
            with self.subTest(check="old resolver fails this fixture"):
                a = self._resolve_old(repo, branch, path)
                b = self._resolve_old(repo, merge, path)
                self.assertNotEqual(a, b, "fixture must reproduce the original bug")
            with self.subTest(check="full history sees both additions"):
                git(repo, "checkout", "-q", branch)
                full = git(repo, "log", "--full-history", "--diff-filter=A",
                           "--format=%H", "--", path).splitlines()
                simple = git(repo, "log", "--diff-filter=A", "--format=%H",
                             "--", path).splitlines()
                self.assertEqual(2, len(full))
                self.assertLess(len(simple), len(full))
            with self.subTest(check="untracked artifact has no provenance"):
                with patch.object(replay_artifact, "REPO", repo):
                    self.assertEqual((None, None), replay_artifact.added_commit(
                        "ab_test_runtime/experiments/none.json"))


if __name__ == "__main__":
    unittest.main()
