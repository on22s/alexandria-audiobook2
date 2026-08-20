"""The derived-file merge scheme, tested against the ways it silently failed.

Five CI failures and repeated manual conflict resolution came from one thing:
RESULTS_INDEX.md, results_index.csv and the two audit JSONs are rebuilt from
other committed content, and every branch that regenerates one rewrites the
same aggregate lines. Any two such branches conflict, always, on lines that
carry no information.

The fix is .gitattributes `merge=ours` plus a post-merge/post-rewrite hook that
rebuilds the file from the merged tree. Both halves are required and BOTH
HALVES FAILED SILENTLY WHILE LOOKING CORRECT during development on 2026-08-20:

  * `.gitignore` line 8 is `.*`, which swallowed `.gitattributes`. `git add -A`
    reported nothing, the file was never committed, and merges kept conflicting
    with no visible cause.
  * The hook inherited GIT_DIR from git with no GIT_WORK_TREE beside it, in
    which state `git rev-parse --show-toplevel` fails - so the helper exited on
    its first line behind a `|| exit 0` and produced no output at all.
  * The helper's own "don't run mid-operation" guard tested for MERGE_HEAD, but
    git still holds MERGE_HEAD *while post-merge runs*, so the guard disabled
    the hook entirely.

Each of those produced a clean, quiet, successful-looking merge carrying a
stale index - the exact failure being fixed. So these tests assert the
properties that were violated, not that the scripts merely exist. Rule 21: the
instrument gets checked on cases whose answer is already known.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
DERIVED = ["RESULTS_INDEX.md", "results_index.csv",
           "ab_test_runtime/audit/artifact_structural_audit.json",
           "app/tests/unit_test_inventory.json"]


class GitAttributesTest(unittest.TestCase):
    def test_gitattributes_is_not_swallowed_by_the_dotfile_ignore_rule(self):
        """`.*` in .gitignore hid it once; a negation must keep it visible."""
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".*", ignore.split("\n"),
                      "the broad dotfile rule this guards against is gone; "
                      "re-check whether the negation below is still needed")
        self.assertIn("!.gitattributes", ignore.split("\n"),
                      ".gitattributes is invisible to `git add -A` without "
                      "this negation, and the merge driver silently does "
                      "nothing")

    def test_gitattributes_is_actually_tracked(self):
        """Not ignored is not the same as committed. Ask git, not the file."""
        out = subprocess.run(["git", "ls-files", "--", ".gitattributes"],
                             cwd=REPO, capture_output=True, text=True)
        self.assertEqual(".gitattributes", out.stdout.strip(),
                         "the file exists but git is not tracking it, so no "
                         "clone or CI checkout has the merge driver")

    def test_every_derived_file_is_marked_merge_ours(self):
        for path in DERIVED:
            with self.subTest(path=path):
                out = subprocess.run(["git", "check-attr", "merge", "--", path],
                                     cwd=REPO, capture_output=True, text=True)
                self.assertIn("merge: ours", out.stdout,
                              f"{path} is rebuilt from committed content, so a "
                              f"textual merge of it is always wrong")


class HookHelperTest(unittest.TestCase):
    """Properties of tools/regen_derived_commit.sh, read as source.

    These are static assertions rather than a live merge because a live merge
    needs a scratch clone and ~9s of regeneration per case. What they pin are
    exactly the three lines whose absence made the hook a no-op.
    """

    def setUp(self):
        self.src = (REPO / "tools" / "regen_derived_commit.sh").read_text(encoding="utf-8")

    def test_it_drops_the_inherited_git_environment(self):
        """GIT_DIR without GIT_WORK_TREE breaks rev-parse --show-toplevel."""
        self.assertRegex(self.src, r"(?m)^unset .*\bGIT_DIR\b")

    def test_it_does_not_bail_out_on_merge_head(self):
        """git still holds MERGE_HEAD while post-merge runs.

        A guard on MERGE_HEAD is not a conservative choice here - it disables
        the hook on every merge, which is the only case it exists for.
        """
        code = re.sub(r"#[^\n]*", "", self.src)   # comments discuss it freely
        self.assertNotIn("MERGE_HEAD", code,
                         "a MERGE_HEAD guard makes this hook a no-op on merges")

    def test_it_commits_with_plumbing_not_git_commit(self):
        """`git commit -- <paths>` dies with 'partial commit during a merge',
        and a pathspec-free `git commit` would see MERGE_HEAD and build a
        second merge commit. commit-tree is immune to both."""
        code = re.sub(r"#[^\n]*", "", self.src)
        self.assertIn("commit-tree", code)
        # `commit-tree` contains "commit", so match the porcelain form exactly.
        self.assertNotRegex(code, r"git [^\n]*\bcommit\b(?!-tree)")

    def test_it_refuses_when_unrelated_changes_are_staged(self):
        """It commits the index, so anything else staged would ride along."""
        code = re.sub(r"#[^\n]*", "", self.src)
        self.assertIn("diff --cached --name-only", code)

    def test_it_is_guarded_against_re_entry(self):
        self.assertIn("ALEXANDRIA_REGEN_HOOK", self.src)


class InheritedGitEnvironmentTest(unittest.TestCase):
    """Every script a hook can reach must drop the git env it inherits.

    This is the bug that bit twice in one hour, in two different scripts, with
    two different symptoms: the merge helper exited silently, and the
    pre-commit path wrote an inventory EMPTY of the module being committed
    while printing "Unit test inventory matches discovery" over it. The second
    is the worse kind - a fallback that returns a plausible answer.
    """

    def test_regen_derived_unsets_it_before_running_git(self):
        src = (REPO / "tools" / "regen_derived.sh").read_text(encoding="utf-8")
        unset_at = src.index("unset GIT_DIR")
        first_git = src.index("git rev-parse")
        self.assertLess(unset_at, first_git,
                        "the unset must precede the first git call, or the "
                        "call it is protecting has already run")

    def test_the_pre_commit_hook_unsets_it_too(self):
        """It runs its own git-backed checks, outside regen_derived.sh."""
        src = (REPO / ".githooks" / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("unset GIT_DIR", src)

    def test_the_merge_helper_unsets_it_too(self):
        src = (REPO / "tools" / "regen_derived_commit.sh").read_text(encoding="utf-8")
        self.assertIn("unset GIT_DIR", src)


class LiveCheckoutTest(unittest.TestCase):
    """The hook must never put a commit on the live tree's main.

    That checkout sits on main at origin/main and runs the GPU queue (Rule 24).
    Its derived files are whatever main has, which CI already validated, so a
    regeneration there can only differ because of artifacts a RUNNING job has
    written and not committed. Committing those would bake in-flight state into
    main and leave an unpushed commit behind.

    Not hypothetical: two such commits accumulated by 2026-08-20 and turned the
    next `git pull --ff-only` into "Not possible to fail-forward", with one of
    them holding the only copy of a chain script. A hook that recreated that on
    every pull would be a worse bug than the conflicts it removes.
    """

    def setUp(self):
        self.src = (REPO / "tools" / "regen_derived_commit.sh").read_text(encoding="utf-8")
        self.code = re.sub(r"#[^\n]*", "", self.src)

    def test_it_stops_when_head_is_exactly_at_upstream(self):
        self.assertIn("@{upstream}", self.code,
                      "without this the hook commits on the live tree's main "
                      "every time a pull moves an artifact")

    def test_the_upstream_check_comes_before_the_regeneration(self):
        """Ordering is the whole point on the live tree: a pull there should
        cost nothing, not nine seconds of regeneration thrown away."""
        upstream_at = self.code.index("@{upstream}")
        regen_at = self.code.index("regen_derived.sh")
        self.assertLess(upstream_at, regen_at,
                        "the guard runs after the work it exists to skip")

    def test_a_branch_ahead_of_upstream_is_not_skipped(self):
        """The guard must test equality, not merely 'has an upstream' - a
        feature branch always has one and always needs the rebuild."""
        self.assertRegex(self.code, r'\[ "\$upstream" = "\$\(git[^)]*rev-parse HEAD\)" \]')


class SingleSourceTest(unittest.TestCase):
    """Rule 15: one answer to "which files are derived and how".

    ready.sh, resolve_generated.sh and both hooks all needed this list. Four
    hand-maintained copies is precisely the drift that has already cost this
    repo an hour on llm_mode/base_url.
    """

    def test_the_derived_list_lives_in_exactly_one_script(self):
        regen = REPO / "tools" / "regen_derived.sh"
        self.assertTrue(regen.exists())
        for caller in ["ready.sh", "resolve_generated.sh",
                       ".githooks/pre-commit", ".githooks/post-merge",
                       ".githooks/post-rewrite"]:
            with self.subTest(caller=caller):
                text = (REPO / caller).read_text(encoding="utf-8")
                self.assertIn("regen_derived", text,
                              f"{caller} must delegate to tools/regen_derived.sh "
                              f"rather than keep its own copy of the list")

    def _run(self, flag):
        return subprocess.run(["bash", str(REPO / "tools" / "regen_derived.sh"), flag],
                              cwd=REPO, capture_output=True, text=True)

    def test_paths_works_without_an_interpreter(self):
        """--paths must answer in ANY checkout, venv or not.

        CI has no app/env, and resolve_generated.sh's refusal path has to work
        there too - a script that cannot say which files are derived until it
        finds a venv fails for the wrong reason, which is the mistake that
        version of resolve_generated.sh already made once.
        """
        out = self._run("--paths")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertTrue(out.stdout.strip(), "--paths printed nothing")

    def test_python_either_answers_or_fails_loudly(self):
        """It must never invent a plausible interpreter.

        This test asserted a bare success and CI failed it on the first run,
        correctly: there is no app/env on a runner. The contract is not "always
        succeeds", it is "prints a real interpreter or refuses with a legible
        reason" - falling back to a bare python3 would get a
        ModuleNotFoundError halfway through a regeneration and read as a broken
        branch ([[Rule 21]]: the dangerous fallback is the one that looks fine).
        """
        out = self._run("--python")
        if out.returncode == 0:
            path = out.stdout.strip()
            self.assertTrue(os.access(path, os.X_OK),
                            f"reported {path!r} but it is not executable")
        else:
            self.assertIn("no interpreter", (out.stderr + out.stdout).lower(),
                          "it failed without saying why")
            self.assertFalse(out.stdout.strip(),
                             "it printed something on stdout while failing; a "
                             "caller doing python=$(... --python) would use it")

    def test_every_path_it_claims_is_derived_exists(self):
        out = subprocess.run(["bash", str(REPO / "tools" / "regen_derived.sh"), "--paths"],
                             cwd=REPO, capture_output=True, text=True)
        for path in out.stdout.split():
            with self.subTest(path=path):
                self.assertTrue((REPO / path).exists(),
                                f"{path} is listed as derived but is not here; "
                                f"a stale entry means the hooks stage nothing "
                                f"for it and it goes quietly out of date")


class InstallerTest(unittest.TestCase):
    def test_the_installer_sets_both_halves(self):
        """.gitattributes alone does nothing: `ours` is not a built-in driver,
        and with merge.ours.driver unset git ignores the attribute silently."""
        src = (REPO / "tools" / "install_git_hooks.sh").read_text(encoding="utf-8")
        self.assertIn("merge.ours.driver", src)
        self.assertIn("core.hooksPath", src)

    def test_ready_installs_the_hooks(self):
        """A checkout where nobody ran the installer has none of this, and the
        conflicts come straight back. ready.sh is the habit that already
        exists, so it is where installation belongs."""
        self.assertIn("install_git_hooks.sh",
                      (REPO / "ready.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
