"""Actual staging callers must preserve the human-owned GOALS index/blob."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests import test_derived_metadata

ROOT = test_derived_metadata.ROOT


class DerivedProseStagingTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def fixture(self, root):
        test_derived_metadata.DerivedMetadataTests().fixture(root)
        for name in ("tools/regen_derived_commit.sh", ".githooks/pre-commit"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        (root / "app/tests/test_fixture.py").write_text("# tracked fixture\n")
        (root / "GOALS.md").write_text("original human prose\n")
        self.git(root, "config", "core.hooksPath", "no-hooks")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "fixture base")

    def run_caller(self, root, caller):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and key != "ALEXANDRIA_REGEN_HOOK"}
        command = ["bash", str(root / ".githooks/pre-commit")] if caller == "pre-commit" else [
            "bash", str(root / "tools/regen_derived_commit.sh"), caller]
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def stage_prose(self, root, mode):
        if mode != "unstaged":
            (root / "GOALS.md").write_text("deliberately staged human prose\n")
            self.git(root, "add", "GOALS.md")
        if mode != "staged":
            (root / "GOALS.md").write_text("unfinished unstaged human prose\n")

    def test_precommit_keeps_unstaged_and_deliberately_staged_prose_boundaries(self):
        for mode in ("unstaged", "staged", "partial"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                self.stage_prose(root, mode)
                (root / "app/tests/test_fixture.py").write_text("# staged change\n")
                self.git(root, "add", "app/tests/test_fixture.py")
                index = self.git(root, "show", ":GOALS.md")
                worktree = (root / "GOALS.md").read_bytes()
                head = self.git(root, "rev-parse", "HEAD")
                self.run_caller(root, "pre-commit")
                self.assertEqual(index, self.git(root, "show", ":GOALS.md"))
                self.assertEqual(worktree, (root / "GOALS.md").read_bytes())
                self.assertEqual(head, self.git(root, "rev-parse", "HEAD"))
                self.assertEqual("resolved", self.git(root, "show", ":RESULTS_INDEX.md"))
                self.assertEqual("original human prose", self.git(root, "show", "HEAD:GOALS.md"))

    def test_followup_commits_only_generated_blobs_with_unstaged_prose(self):
        for caller in ("post-merge", "post-rewrite"):
            with self.subTest(caller=caller), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                self.stage_prose(root, "unstaged")
                head = self.git(root, "rev-parse", "HEAD")
                self.run_caller(root, caller)
                self.assertNotEqual(head, self.git(root, "rev-parse", "HEAD"))
                self.assertEqual(head, self.git(root, "rev-parse", "HEAD^"))
                self.assertEqual("RESULTS_INDEX.md", self.git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"))
                self.assertEqual("original human prose", self.git(root, "show", "HEAD:GOALS.md"))
                self.assertEqual("original human prose", self.git(root, "show", ":GOALS.md"))
                self.assertEqual("unfinished unstaged human prose\n", (root / "GOALS.md").read_text())

    def test_followup_refuses_staged_prose_without_changing_index_or_head(self):
        for caller in ("post-merge", "post-rewrite"):
            for mode in ("staged", "partial"):
                with self.subTest(caller=caller, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.fixture(root)
                    self.stage_prose(root, mode)
                    head = self.git(root, "rev-parse", "HEAD")
                    index = self.git(root, "write-tree")
                    worktree = (root / "GOALS.md").read_bytes()
                    result = self.run_caller(root, caller)
                    self.assertIn("other changes are staged", result.stderr)
                    self.assertEqual(head, self.git(root, "rev-parse", "HEAD"))
                    self.assertEqual(index, self.git(root, "write-tree"))
                    self.assertEqual(worktree, (root / "GOALS.md").read_bytes())
                    self.assertEqual("resolved\n", (root / "RESULTS_INDEX.md").read_text())


if __name__ == "__main__":
    unittest.main()
