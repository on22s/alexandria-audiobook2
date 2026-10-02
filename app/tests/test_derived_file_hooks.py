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



class DerivedPathFailureArtifactTests(unittest.TestCase):
    def test_missing_or_failed_path_list_cannot_commit_unrelated_staged_work(self):
        import shutil
        import tempfile
        for mode in ('exit 7', "printf '\\n \\t\\n'; exit 0", "echo derived.txt; exit 7"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                def git(*args):
                    return subprocess.run(['git',*args],cwd=repo,capture_output=True,text=True,check=True).stdout.strip()
                git('init','-q')
                git('config','user.name','Hook Fixture')
                git('config','user.email','fixture@example.invalid')
                git('config','core.hooksPath',str(repo/'no-hooks'))
                (repo/'derived.txt').write_text('old index\n')
                (repo/'unrelated.txt').write_text('original work\n')
                git('add','derived.txt','unrelated.txt');git('commit','-qm','Fixture')
                before_head=git('rev-parse','HEAD')
                (repo/'unrelated.txt').write_text('user staged change\n');git('add','unrelated.txt')
                before_index=git('write-tree')
                tools=repo/'tools';tools.mkdir()
                helper=tools/'regen_derived_commit.sh'
                shutil.copy2(REPO/'tools/regen_derived_commit.sh',helper)
                regen=tools/'regen_derived.sh'
                regen.write_text('#!/bin/bash\nif [ "$1" = "--paths" ]; then\n'+mode+'\nfi\nprintf "new index\\n" > derived.txt\n')
                regen.chmod(0o755)
                environment={key:value for key,value in os.environ.items()
                             if not key.startswith('GIT_') and key!='ALEXANDRIA_REGEN_HOOK'}
                result=subprocess.run(['bash',str(helper),'fixture-hook'],cwd=repo,env=environment,
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(0,result.returncode,result.stderr)
                self.assertEqual(before_head,git('rev-parse','HEAD'),'Path-list failure committed user work')
                self.assertEqual(before_index,git('write-tree'),'Path-list failure modified user staging')
                self.assertEqual('user staged change\n',(repo/'unrelated.txt').read_text())
                self.assertEqual('new index\n',(repo/'derived.txt').read_text())
                self.assertIn('derived file paths',result.stderr)


    def test_valid_path_list_commits_only_derived_artifact_and_keeps_user_work_dirty(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            def git(*args):
                return subprocess.run(['git',*args],cwd=repo,capture_output=True,text=True,check=True).stdout.strip()
            git('init','-q');git('config','user.name','Hook Fixture');git('config','user.email','fixture@example.invalid')
            git('config','core.hooksPath',str(repo/'no-hooks'))
            (repo/'derived.txt').write_text('old index\n');(repo/'unrelated.txt').write_text('original work\n')
            git('add','derived.txt','unrelated.txt');git('commit','-qm','Fixture')
            original=git('rev-parse','HEAD')
            (repo/'unrelated.txt').write_text('user unstaged change\n')
            tools=repo/'tools';tools.mkdir()
            helper=tools/'regen_derived_commit.sh';shutil.copy2(REPO/'tools/regen_derived_commit.sh',helper)
            regen=tools/'regen_derived.sh'
            regen.write_text('#!/bin/bash\nif [ "$1" = "--paths" ]; then echo derived.txt; exit 0; fi\nprintf "new index\\n" > derived.txt\n')
            regen.chmod(0o755)
            environment={key:value for key,value in os.environ.items()
                         if not key.startswith('GIT_') and key!='ALEXANDRIA_REGEN_HOOK'}
            result=subprocess.run(['bash',str(helper),'fixture-hook'],cwd=repo,env=environment,
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertNotEqual(original,git('rev-parse','HEAD'))
            self.assertEqual(original,git('rev-parse','HEAD^'))
            self.assertEqual('new index',git('show','HEAD:derived.txt'))
            self.assertEqual('original work',git('show','HEAD:unrelated.txt'))
            self.assertEqual('user unstaged change\n',(repo/'unrelated.txt').read_text())
            self.assertEqual('derived.txt',git('diff-tree','--no-commit-id','--name-only','-r','HEAD'))
            self.assertEqual('',git('diff','--cached','--name-only'))
            self.assertIn('rebuilt derived files',result.stdout)


class DerivedCommitFailureTests(unittest.TestCase):
    def make_repo(self, root):
        from pathlib import Path
        tools = root / "tools"
        tools.mkdir()
        helper = tools / "regen_derived_commit.sh"
        helper.write_bytes((REPO / "tools/regen_derived_commit.sh").read_bytes())
        helper.chmod(0o755)
        producer = tools / "regen_derived.sh"
        producer.write_text("#!/bin/bash\nif [ \"${1:-}\" = \"--paths\" ]; then\n"
                            "  printf '%s\\n' RESULTS_INDEX.md results_index.csv\nfi\n", encoding="utf-8")
        producer.chmod(0o755)
        (root / "RESULTS_INDEX.md").write_text("old index\n")
        (root / "results_index.csv").write_text("old csv\n")
        (root / "unrelated_RESULTS_INDEX.md").write_text("foreign old\n")
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        self.git(root, "config", "core.hooksPath", "/dev/null")
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "fixture base")
        return helper

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_foreign_staged_path_containing_derived_name_cannot_ride_in_commit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self.make_repo(root)
            before = self.git(root, "rev-parse", "HEAD")
            (root / "RESULTS_INDEX.md").write_text("new index\n")
            (root / "unrelated_RESULTS_INDEX.md").write_text("foreign new\n")
            self.git(root, "add", "unrelated_RESULTS_INDEX.md")
            result = subprocess.run(["bash", str(helper), "fixture"], cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, self.git(root, "rev-parse", "HEAD"))
            self.assertIn("other changes are staged", result.stderr)
            self.assertEqual("foreign old", self.git(root, "show", "HEAD:unrelated_RESULTS_INDEX.md"))
            self.assertEqual("foreign new", self.git(root, "show", ":unrelated_RESULTS_INDEX.md"))
            self.assertEqual("old index", self.git(root, "show", ":RESULTS_INDEX.md"))

    def test_staging_failure_stops_before_commit_tree_and_preserves_original_head(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self.make_repo(root)
            before = self.git(root, "rev-parse", "HEAD")
            (root / "RESULTS_INDEX.md").write_text("new index\n")
            (root / "results_index.csv").write_text("new csv\n")
            executable = root / "bin" / "git"
            executable.parent.mkdir()
            executable.write_text(
                '#!/bin/bash\nprintf "%s\\n" "$*" >> "$HOOK_TEST_COMMANDS"\n'
                'if [ "${3:-}" = "add" ]; then\n'
                '  "$HOOK_TEST_GIT" -C "$2" add -- RESULTS_INDEX.md || exit $?\n'
                '  echo "fixture staging failure" >&2\n  exit 57\nfi\n'
                'exec "$HOOK_TEST_GIT" "$@"\n', encoding="utf-8")
            executable.chmod(0o755)
            commands = root / "commands.log"
            environment = os.environ.copy()
            environment.update(PATH=str(executable.parent) + os.pathsep + environment["PATH"],
                               HOOK_TEST_GIT=shutil.which("git"), HOOK_TEST_COMMANDS=str(commands))
            result = subprocess.run(["bash", str(helper), "fixture"], cwd=root, env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, self.git(root, "rev-parse", "HEAD"))
            self.assertIn("could not stage derived files", result.stderr)
            calls = commands.read_text().splitlines()
            self.assertFalse(any("commit-tree" in line for line in calls), calls)
            self.assertEqual("new index", self.git(root, "show", ":RESULTS_INDEX.md"))
            self.assertEqual("old csv", self.git(root, "show", ":results_index.csv"))
            self.assertEqual("old index", self.git(root, "show", "HEAD:RESULTS_INDEX.md"))

    def test_exact_derived_staged_paths_and_working_changes_commit_only_rebuild(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self.make_repo(root)
            before = self.git(root, "rev-parse", "HEAD")
            (root / "RESULTS_INDEX.md").write_text("new index\n")
            self.git(root, "add", "RESULTS_INDEX.md")
            (root / "results_index.csv").write_text("new csv\n")
            result = subprocess.run(["bash", str(helper), "fixture"], cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotEqual(before, self.git(root, "rev-parse", "HEAD"))
            self.assertEqual(before, self.git(root, "rev-parse", "HEAD^"))
            self.assertEqual("new index", self.git(root, "show", "HEAD:RESULTS_INDEX.md"))
            self.assertEqual("new csv", self.git(root, "show", "HEAD:results_index.csv"))
            self.assertEqual("foreign old", self.git(root, "show", "HEAD:unrelated_RESULTS_INDEX.md"))
            self.assertEqual("", self.git(root, "diff", "--cached", "--name-only"))


class DerivedCommitDetectorTests(unittest.TestCase):
    make_repo = DerivedCommitFailureTests.make_repo
    git = DerivedCommitFailureTests.git

    def make_audit_repo(self, root):
        helper = self.make_repo(root)
        audit = root / "ab_test_runtime" / "audit"
        audit.mkdir(parents=True)
        (audit / "old.json").write_text('{"old": true}\n')
        producer = root / "tools" / "regen_derived.sh"
        producer.write_text("#!/bin/bash\nif [ \"${1:-}\" = \"--paths\" ]; then\n"
                            "  printf '%s\\n' RESULTS_INDEX.md results_index.csv ab_test_runtime/audit\nfi\n")
        self.git(root, "add", "tools/regen_derived.sh", "ab_test_runtime/audit/old.json")
        self.git(root, "commit", "-qm", "audit fixture base")
        return helper, audit

    def test_staged_child_of_derived_directory_is_allowed_but_similar_directory_is_not(self):
        import tempfile
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper, audit = self.make_audit_repo(root)
                before = self.git(root, "rev-parse", "HEAD")
                (root / "RESULTS_INDEX.md").write_text("new index\n")
                target = (root / "ab_test_runtime" / "audit_extra") if foreign else audit
                target.mkdir(exist_ok=True)
                new = target / "new.json"
                new.write_text('{"new": true}\n')
                self.git(root, "add", str(new.relative_to(root)))
                result = subprocess.run(["bash", str(helper), "fixture"], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                if foreign:
                    self.assertEqual(before, self.git(root, "rev-parse", "HEAD"))
                    self.assertIn("other changes are staged", result.stderr)
                    self.assertEqual(str(new.relative_to(root)), self.git(root, "diff", "--cached", "--name-only"))
                else:
                    self.assertNotEqual(before, self.git(root, "rev-parse", "HEAD"))
                    self.assertEqual('{"new": true}', self.git(root, "show", "HEAD:ab_test_runtime/audit/new.json"))
                    self.assertEqual("new index", self.git(root, "show", "HEAD:RESULTS_INDEX.md"))

    def test_new_untracked_and_already_staged_derived_outputs_trigger_real_commit(self):
        import tempfile
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper, audit = self.make_audit_repo(root)
                before = self.git(root, "rev-parse", "HEAD")
                new = audit / "new.json"
                new.write_text('{"new": true}\n')
                if staged:
                    self.git(root, "add", str(new.relative_to(root)))
                self.assertEqual("", self.git(root, "diff", "--name-only"))
                result = subprocess.run(["bash", str(helper), "fixture"], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotEqual(before, self.git(root, "rev-parse", "HEAD"))
                self.assertEqual(before, self.git(root, "rev-parse", "HEAD^"))
                self.assertEqual('{"new": true}', self.git(root, "show", "HEAD:ab_test_runtime/audit/new.json"))
                self.assertEqual("", self.git(root, "diff", "--cached", "--name-only"))

    def test_clean_derived_state_does_not_commit_unrelated_working_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, audit = self.make_audit_repo(root)
            before = self.git(root, "rev-parse", "HEAD")
            (root / "unrelated_RESULTS_INDEX.md").write_text("foreign new\n")
            result = subprocess.run(["bash", str(helper), "fixture"], cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, self.git(root, "rev-parse", "HEAD"))
            self.assertEqual("", self.git(root, "diff", "--cached", "--name-only"))
            self.assertEqual("foreign new\n", (root / "unrelated_RESULTS_INDEX.md").read_text())


    def test_git_inspection_failure_preserves_head_and_the_entire_index(self):
        import shutil
        import tempfile
        for command in ("status", "diff"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper, audit = self.make_audit_repo(root)
                (root / "RESULTS_INDEX.md").write_text("new index\n")
                (audit / "new.json").write_text('{"new": true}\n')
                self.git(root, "add", "ab_test_runtime/audit/new.json")
                before_head = self.git(root, "rev-parse", "HEAD")
                before_index = self.git(root, "write-tree")
                executable = root / "bin" / "git"
                executable.parent.mkdir()
                executable.write_text(
                    '#!/bin/bash\nprintf "%s\\n" "$*" >> "$HOOK_TEST_COMMANDS"\n'
                    'if [ "${3:-}" = "$HOOK_TEST_FAIL" ]; then\n'
                    '  echo "fixture inspection failure" >&2\n  exit 58\nfi\n'
                    'exec "$HOOK_TEST_GIT" "$@"\n', encoding="utf-8")
                executable.chmod(0o755)
                commands = root / "commands.log"
                environment = os.environ.copy()
                environment.update(PATH=str(executable.parent) + os.pathsep + environment["PATH"],
                                   HOOK_TEST_GIT=shutil.which("git"), HOOK_TEST_COMMANDS=str(commands),
                                   HOOK_TEST_FAIL=command)
                result = subprocess.run(["bash", str(helper), "fixture"], cwd=root, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("could not inspect", result.stderr)
                self.assertEqual(before_head, self.git(root, "rev-parse", "HEAD"))
                self.assertEqual(before_index, self.git(root, "write-tree"))
                self.assertFalse(any("commit-tree" in line or " add " in line
                                     for line in commands.read_text().splitlines()))


class ReadyStagingGateTests(unittest.TestCase):
    def _fixture(self, repo):
        import shutil
        for name in ("tools", "app/tests", "ab_test_runtime/audit"):
            (repo / name).mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "ready.sh", repo / "ready.sh")
        scripts = {
            "tools/install_git_hooks.sh": "#!/bin/bash\nexit 0\n",
            "tools/regen_derived.sh": '#!/bin/bash\nif [ "${1:-}" = "--python" ]; then echo "$(dirname "$0")/fixture-python"; fi\n',
            "tools/fixture-python": '#!/bin/bash\ncase "$1" in *verify_release.py) touch "$(dirname "$0")/../verifier-ran";; esac\nexit 0\n',
        }
        for name, text in scripts.items():
            (repo / name).write_text(text)
            (repo / name).chmod(0o755)
        (repo / "app/tests/unit_test_inventory.json").write_text('{"old": []}\n')
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Ready Fixture"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "ready@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.hooksPath", "no-hooks"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Fixture"], cwd=repo, check=True)

    def test_untracked_and_unstaged_derived_files_refuse_before_release_verifier(self):
        import tempfile
        for kind in ("new_inventory", "new_audit", "modified", "deleted"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._fixture(repo)
                inventory = repo / "app/tests/unit_test_inventory.json"
                if kind == "new_inventory":
                    subprocess.run(["git", "rm", "--cached", str(inventory)], cwd=repo, check=True, capture_output=True)
                elif kind == "new_audit":
                    (repo / "ab_test_runtime/audit/new.json").write_text('{"artifact": "new"}')
                elif kind == "modified":
                    inventory.write_text('{"new": []}')
                else:
                    inventory.unlink()
                before_index = subprocess.check_output(["git", "write-tree"], cwd=repo)
                before_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo)
                result = subprocess.run(["bash", str(repo / "ready.sh")], cwd=repo, capture_output=True, text=True)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("NOT READY", result.stdout)
                self.assertFalse((repo / "verifier-ran").exists())
                self.assertEqual(before_index, subprocess.check_output(["git", "write-tree"], cwd=repo))
                self.assertEqual(before_head, subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo))

    def test_clean_or_fully_staged_derived_changes_reach_verifier_despite_unrelated_untracked_work(self):
        import tempfile
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._fixture(repo)
                (repo / "unrelated.py").write_text("user work")
                if staged:
                    (repo / "app/tests/unit_test_inventory.json").write_text('{"new": []}')
                    (repo / "ab_test_runtime/audit/new.json").write_text('{"artifact": "new"}')
                    subprocess.run(["git", "add", "app/tests/unit_test_inventory.json", "ab_test_runtime/audit/new.json"], cwd=repo, check=True)
                before_index = subprocess.check_output(["git", "write-tree"], cwd=repo)
                result = subprocess.run(["bash", str(repo / "ready.sh")], cwd=repo, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue((repo / "verifier-ran").is_file())
                self.assertEqual("user work", (repo / "unrelated.py").read_text())
                self.assertEqual(before_index, subprocess.check_output(["git", "write-tree"], cwd=repo))

    def test_git_status_failure_cannot_report_a_clean_staging_gate(self):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._fixture(repo)
            bin_dir = repo / "bin"
            bin_dir.mkdir()
            git = shutil.which("git")
            (bin_dir / "git").write_text('#!/bin/bash\nif [ "${3:-}" = "status" ]; then exit 61; fi\nexec "' + git + '" "$@"\n')
            (bin_dir / "git").chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(repo / "ready.sh")], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("cannot inspect", result.stderr)
            self.assertFalse((repo / "verifier-ran").exists())
