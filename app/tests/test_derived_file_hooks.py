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

from tests import test_derived_metadata
from tests.test_support import get_bash_executable

REPO = Path(__file__).parent.parent.parent
DERIVED = ["RESULTS_INDEX.md", "results_index.csv",
           "ab_test_runtime/audit/artifact_structural_audit.json",
           "app/tests/unit_test_inventory.json"]


def posix_path(path):
    """Git Bash only searches POSIX-style PATH entries (C:/x -> /c/x)."""
    text = str(path).replace(os.sep, "/")
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def bash_path(path):
    """One quoted, forward-slash word for a bash command string."""
    return "'" + str(path).replace(os.sep, "/").replace("'", "'\"'\"'") + "'"


def run_with_fake_git(fake_bin, command, cwd, environment):
    """Run a bash command with fake_bin FIRST on the shell's own PATH.

    MEASURED on Windows: Git Bash prepends its /usr/bin to whatever PATH the
    process inherited, so a fake `git` handed over in env=... is never reached
    - the failure injection then runs the real git, and the test asserts an
    error the script never saw. The ordering only holds when PATH is set
    inside the shell that runs the script, which is the same technique
    InterpreterResolutionTest uses for its fake `git`.
    """
    script = 'PATH="%s:$PATH"; export PATH; %s' % (posix_path(fake_bin), command)
    return subprocess.run([get_bash_executable(), "--noprofile", "--norc", "-c", script],
                          cwd=cwd, env=environment, capture_output=True,
                          text=True, timeout=20)


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
        # This pins AUTOMATIC resolution in this checkout. An AA2_PYTHON
        # inherited from the caller is a different subject - InterpreterResolutionTest
        # owns the explicit-override cases - and an invalid one would make the
        # script refuse for a reason this test does not assert.
        return subprocess.run([get_bash_executable(), str(REPO / "tools" / "regen_derived.sh"), flag],
                              cwd=REPO, capture_output=True, text=True,
                              env=test_derived_metadata.fixture_environment())

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
        out = subprocess.run([get_bash_executable(), str(REPO / "tools" / "regen_derived.sh"), "--paths"],
                             cwd=REPO, capture_output=True, text=True)
        for path in out.stdout.split():
            with self.subTest(path=path):
                self.assertTrue((REPO / path).exists(),
                                f"{path} is listed as derived but is not here; "
                                f"a stale entry means the hooks stage nothing "
                                f"for it and it goes quietly out of date")


class InterpreterResolutionTest(unittest.TestCase):
    """The interpreter resolver, run against the real script in a tiny repo.

    Every fixture is a local `git init` under TemporaryDirectory - no clone, no
    venv, no generator. `--python` and `--metadata` stop before the generators,
    so the fake interpreter only has to be a file with the executable bit.

    The cases are the ones that were wrong or untested: a reusable interpreter
    outside app/env chosen explicitly, a bad value refused instead of silently
    falling back, the Windows Scripts/ layout, and the worktree path with a
    space in it that `awk '{print $2}'` truncated.
    """

    def _git(self, repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def _repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Interpreter Fixture")
        self._git(repo, "config", "user.email", "fixture@example.invalid")
        self._git(repo, "config", "core.hooksPath", "no-hooks")
        return repo

    def _fake_python(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def _forward(path):
        """Git and the script print drive paths with forward slashes."""
        return str(path).replace(os.sep, "/")

    @staticmethod
    def _posix(path):
        """Git Bash only searches POSIX-style PATH entries (C:/x -> /c/x)."""
        return posix_path(path)

    def _run(self, repo, arguments, extra_env=None):
        environment = test_derived_metadata.fixture_environment(extra_env)
        arguments = [arguments] if isinstance(arguments, str) else list(arguments)
        return subprocess.run(
            [get_bash_executable(), str(REPO / "tools" / "regen_derived.sh"), *arguments],
            cwd=repo, env=environment, capture_output=True, text=True,
            timeout=20)

    def test_explicit_override_is_used_verbatim_and_not_the_local_venv(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            local = self._fake_python(repo / "app" / "env" / "bin" / "python")
            override = self._fake_python(root / "override python")
            out = self._run(repo, "--python", {"AA2_PYTHON": str(override)})
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual(str(override), out.stdout.strip())
            self.assertNotEqual(str(local), out.stdout.strip())

    def test_relative_override_survives_generator_directory_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            caller = repo / "caller"
            caller.mkdir()
            decoy = self._fake_python(repo / "app/env/bin/python")
            decoy.write_text('#!/bin/sh\nexit 64\n', encoding="utf-8")
            interpreter = self._fake_python(caller / "app/env/bin/python")
            interpreter.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AA2_TEST_CALLS"\n',
                encoding="utf-8")
            marker = repo / "calls"
            environment = {"AA2_PYTHON": "app/env/bin/python",
                           "AA2_TEST_CALLS": str(marker)}
            query = self._run(caller, "--python", environment)
            self.assertEqual(0, query.returncode, query.stderr)
            self.assertTrue(query.stdout.strip().startswith("/"), query.stdout)
            self.assertTrue(query.stdout.strip().endswith(
                "/" + interpreter.relative_to(repo).as_posix()), query.stdout)
            result = self._run(caller, "--quiet", environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = marker.read_text(encoding="utf-8").splitlines()
            self.assertEqual(5, len(calls), calls)
            self.assertEqual("update_test_inventory.py", calls[-1])

    def test_pre_commit_uses_the_shared_explicit_interpreter(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            tools = repo / "tools"
            tools.mkdir()
            shutil.copyfile(REPO / "tools/regen_derived.sh",
                            tools / "regen_derived.sh")
            (tools / "regen_derived.sh").chmod(0o755)
            interpreter = self._fake_python(root / "override python")
            interpreter.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AA2_TEST_CALLS"\n',
                encoding="utf-8")
            (repo / "app").mkdir()
            marker = root / "calls"
            environment = test_derived_metadata.fixture_environment({
                "AA2_PYTHON": str(interpreter),
                "AA2_TEST_CALLS": str(marker),
            })
            result = subprocess.run(
                [get_bash_executable(), str(REPO / ".githooks/pre-commit")], cwd=repo,
                env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(marker.exists(), result.stdout + result.stderr)
            self.assertEqual(["update_api_contract_snapshots.py --check",
                              "update_test_inventory.py --check"],
                             marker.read_text(encoding="utf-8").splitlines())

    def test_invalid_explicit_override_refuses_instead_of_falling_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            self._fake_python(repo / "app" / "env" / "bin" / "python")
            directory = root / "a directory"
            directory.mkdir()
            values = {
                "missing path": str(root / "no such python"),
                "directory": str(directory),
                "shell command": "python3 -c pass",
            }
            for label, value in values.items():
                for flag in ("--python", "--metadata"):
                    with self.subTest(value=label, flag=flag):
                        out = self._run(repo, flag, {"AA2_PYTHON": value})
                        self.assertNotEqual(0, out.returncode,
                                            "a bad override silently resolved")
                        self.assertEqual("", out.stdout.strip(),
                                         "it printed a fallback interpreter")
                        self.assertIn("AA2_PYTHON", out.stderr)
                        self.assertIn("not an executable file", out.stderr)

    def test_no_interpreter_refuses_and_invents_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            for flag in ("--python", "--metadata"):
                with self.subTest(flag=flag):
                    out = self._run(repo, flag)
                    self.assertNotEqual(0, out.returncode)
                    self.assertEqual("", out.stdout.strip())
                    self.assertIn("no interpreter", out.stderr.lower())

    def test_paths_answers_before_interpreter_resolution(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            for label, extra_env in (
                    ("no interpreter", None),
                    ("an invalid override", {"AA2_PYTHON": "python3 -c pass"})):
                with self.subTest(case=label):
                    out = self._run(repo, "--paths", extra_env)
                    self.assertEqual(0, out.returncode, out.stderr)
                    self.assertEqual(
                        ["RESULTS_INDEX.md", "results_index.csv",
                         "LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md",
                         "ab_test_runtime/audit/artifact_structural_audit.json",
                         "ab_test_runtime/audit/legacy_attribution_audit.json",
                         "ab_test_runtime/audit/goal_evidence_audit.json",
                         "app/tests/unit_test_inventory.json"],
                        out.stdout.split())

    def test_automatic_resolution_accepts_windows_scripts_and_prefers_posix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            windows = self._fake_python(
                repo / "app" / "env" / "Scripts" / "python.exe")
            out = self._run(repo, "--python")
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual(self._forward(windows), out.stdout.strip())

            posix = self._fake_python(repo / "app" / "env" / "bin" / "python")
            out = self._run(repo, "--python")
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual(self._forward(posix), out.stdout.strip(),
                             "the POSIX venv layout must stay the first choice")

    def test_main_checkout_path_with_spaces_is_not_truncated(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            main = root / "main checkout with spaces"
            interpreter = self._fake_python(
                main / "app" / "env" / "bin" / "python")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            wrapper = fake_bin / "git"
            wrapper.write_text(
                '#!/bin/bash\n'
                'case " $* " in\n'
                '  *" worktree list --porcelain "*)\n'
                '    printf \'worktree %s\\n\' "$AA2_TEST_MAIN"\n'
                '    exit 0\n'
                '    ;;\n'
                'esac\n'
                'exec "$AA2_TEST_REAL_GIT" "$@"\n',
                encoding="utf-8")
            wrapper.chmod(0o755)
            environment = test_derived_metadata.fixture_environment({
                "AA2_TEST_MAIN": self._forward(main),
                "AA2_TEST_REAL_GIT": shutil.which("git"),
            })
            # Git Bash puts its own /usr/bin ahead of any inherited PATH, so a
            # fake `git` is only first when PATH is set inside the shell that
            # sources the script. Sourcing is still the real script.
            command = ('PATH="%s:$PATH"; export PATH; . "%s" --python'
                       % (self._posix(fake_bin),
                          self._posix(REPO / "tools" / "regen_derived.sh")))
            out = subprocess.run(
                [get_bash_executable(), "--noprofile", "--norc", "-c", command],
                cwd=repo, env=environment, capture_output=True, text=True,
                timeout=20)
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual(self._forward(interpreter), out.stdout.strip())
            self.assertIn("main checkout with spaces", out.stdout)
            interpreter.unlink()
            windows = self._fake_python(
                main / "app" / "env" / "Scripts" / "python.exe")
            out = subprocess.run(
                [get_bash_executable(), "--noprofile", "--norc", "-c", command],
                cwd=repo, env=environment, capture_output=True, text=True,
                timeout=20)
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual(self._forward(windows), out.stdout.strip())

    def test_invalid_override_and_absent_interpreter_have_distinct_exit_codes(self):
        """A caller must be able to tell "misconfigured" from "no venv here".

        Both used to exit 1, so the pre-commit hook could not tell a typo in
        AA2_PYTHON from a checkout that simply has no venv - and treated the
        first as the second.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            for flag in ("--python", "--metadata"):
                with self.subTest(flag=flag, case="absent"):
                    out = self._run(repo, flag)
                    self.assertEqual(1, out.returncode, out.stderr)
                    self.assertIn("no interpreter", out.stderr.lower())
                with self.subTest(flag=flag, case="invalid"):
                    out = self._run(repo, flag, {"AA2_PYTHON": "python3 -c pass"})
                    self.assertEqual(2, out.returncode, out.stderr)
                    self.assertIn("AA2_PYTHON", out.stderr)

    def pre_commit_fixture(self, root, interpreter=None):
        """A disposable repo holding the real hook's own resolver and paths."""
        import shutil
        repo = self._repo(root)
        tools = repo / "tools"
        tools.mkdir()
        shutil.copyfile(REPO / "tools/regen_derived.sh", tools / "regen_derived.sh")
        (tools / "regen_derived.sh").chmod(0o755)
        (repo / "app").mkdir()
        if interpreter is not None:
            self._fake_python(root / "override python").write_text(interpreter, encoding="utf-8")
        return repo

    def run_pre_commit(self, repo, extra_env):
        return subprocess.run(
            [get_bash_executable(), str(REPO / ".githooks/pre-commit")], cwd=repo,
            env=test_derived_metadata.fixture_environment(extra_env),
            capture_output=True, text=True, timeout=20)

    def test_invalid_override_blocks_the_commit_instead_of_skipping_the_checks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "calls"
            repo = self.pre_commit_fixture(root)
            result = self.run_pre_commit(repo, {"AA2_PYTHON": "python3 -c pass",
                                                "AA2_TEST_CALLS": str(marker)})
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("fix or unset AA2_PYTHON", result.stderr)
            self.assertNotIn("skipping snapshot checks", result.stderr)
            self.assertFalse(marker.exists(), "a bad override ran the checks anyway")

    def test_absent_interpreter_skips_both_checks_with_an_explicit_not_verified_notice(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "calls"
            repo = self.pre_commit_fixture(root)
            result = self.run_pre_commit(repo, {"AA2_TEST_CALLS": str(marker)})
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("NOT verified", result.stderr)
            self.assertIn("API contract snapshots", result.stderr)
            self.assertIn("unit-test inventory", result.stderr)
            self.assertIn("CI runs both", result.stderr)
            self.assertFalse(marker.exists(), "it claimed to skip and ran them anyway")

    def test_unverifiable_inventory_is_not_reported_as_drift(self):
        """The pair that must never be merged: 2 cannot verify, 1 drift.

        Before this, every non-zero from the inventory check was printed as
        "unit-test inventory drift" - including a traceback, where nothing had
        been compared at all.
        """
        import tempfile
        interpreter = (
            '#!/bin/sh\n'
            'printf "%s\\n" "$*" >> "$AA2_TEST_CALLS"\n'
            'case "$1" in\n'
            '  update_api_contract_snapshots.py) exit 0 ;;\n'
            '  update_test_inventory.py) exit "$AA2_TEST_INVENTORY_STATUS" ;;\n'
            'esac\n'
            'exit 0\n')
        for status, expected in ((2, "could not be verified"), (1, "drift")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                marker = root / "calls"
                repo = self.pre_commit_fixture(root, interpreter=interpreter)
                result = self.run_pre_commit(repo, {
                    "AA2_PYTHON": str(root / "override python"),
                    "AA2_TEST_CALLS": str(marker),
                    "AA2_TEST_INVENTORY_STATUS": str(status)})
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(expected, result.stderr)
                if status == 2:
                    self.assertNotIn("drift", result.stderr)
                self.assertEqual(
                    ["update_api_contract_snapshots.py --check",
                     "update_test_inventory.py --check"],
                    marker.read_text(encoding="utf-8").splitlines())

    def test_invalid_override_during_regeneration_is_fatal_and_rebuilds_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            tools = repo / "tools"
            tools.mkdir()
            producer = tools / "regen_derived.sh"
            producer.write_text(
                '#!/bin/sh\ncase "${1:-}" in\n'
                '  --quiet) exit 2 ;;\n'
                '  --python) printf "/nonexistent\\n" ; exit 0 ;;\n'
                '  --paths) printf "RESULTS_INDEX.md\\n" ;;\n'
                'esac\nexit 0\n', encoding="utf-8")
            producer.chmod(0o755)
            (repo / "app" / "tests").mkdir(parents=True)
            (repo / "app" / "tests" / "test_probe.py").write_text(
                "import unittest\n", encoding="utf-8")
            self._git(repo, "add", "app/tests/test_probe.py")
            marker = root / "calls"
            result = self.run_pre_commit(repo, {"AA2_TEST_CALLS": str(marker)})
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("no derived file was rebuilt", result.stderr)
            self.assertIn("fix or unset AA2_PYTHON", result.stderr)
            self.assertNotIn("CI will check them", result.stderr)
            self.assertFalse(marker.exists(), "the checks ran after a fatal regeneration")

    def test_regeneration_keeps_the_inventory_tools_own_diagnostics(self):
        """The inventory's stderr is how a missing import reaches the operator.

        Measured on a host without fcntl: discarding stderr left
        "regen_derived: update_test_inventory FAILED" as the only symptom, and
        the tool's own "cannot verify / missing module fcntl" lines - the ones
        that name the fix - were gone. The other generators keep 2>&1.
        """
        source = (REPO / "tools/regen_derived.sh").read_text(encoding="utf-8")
        self.assertNotIn("update_test_inventory.py >/dev/null 2>&1", source)
        self.assertIn("update_test_inventory.py >/dev/null )", source)

    def test_metadata_prints_one_interpreter_and_paths_without_generators(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            marker = root / "interpreter-ran"
            interpreter = repo / "app" / "env" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text(
                '#!/bin/sh\nprintf "ran" > "$AA2_TEST_MARKER"\n',
                encoding="utf-8")
            interpreter.chmod(0o755)
            out = self._run(repo, "--metadata", {
                "AA2_TEST_MARKER": str(marker),
            })
            self.assertEqual(0, out.returncode, out.stderr)
            lines = out.stdout.splitlines()
            self.assertEqual(self._forward(interpreter), lines[0])
            self.assertEqual(7, len(lines) - 1,
                             "--metadata must still print the full derived set")
            self.assertIn("RESULTS_INDEX.md", lines[1:])
            self.assertIn("app/tests/unit_test_inventory.json", lines[1:])
            self.assertFalse(marker.exists(),
                             "--metadata ran a generator; it must only report")
            for arguments in (("--metdata",), ("--quiet", "extra"),
                              ("--python", "extra"), ("--metadata", "extra"),
                              ("--paths", "extra"), ("--quiet", "--quiet")):
                with self.subTest(arguments=arguments):
                    marker.unlink(missing_ok=True)
                    invalid = self._run(repo, arguments, {
                        "AA2_TEST_MARKER": str(marker),
                    })
                    self.assertEqual(2, invalid.returncode,
                                     invalid.stdout + invalid.stderr)
                    self.assertIn("Usage:", invalid.stderr)
                    self.assertEqual("", invalid.stdout)
                    self.assertFalse(marker.exists(),
                                     "invalid arguments ran a generator")


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
                result=subprocess.run([get_bash_executable(),str(helper),'fixture-hook'],cwd=repo,env=environment,
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
            result=subprocess.run([get_bash_executable(),str(helper),'fixture-hook'],cwd=repo,env=environment,
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
            result = subprocess.run([get_bash_executable(), str(helper), "fixture"], cwd=root,
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
            environment.update(HOOK_TEST_GIT=shutil.which("git"), HOOK_TEST_COMMANDS=str(commands))
            result = run_with_fake_git(executable.parent,
                                       "bash %s fixture" % bash_path(helper),
                                       root, environment)
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
            result = subprocess.run([get_bash_executable(), str(helper), "fixture"], cwd=root,
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
        (audit / "artifact_structural_audit.json").write_text('{"old": true}\n')
        producer = root / "tools" / "regen_derived.sh"
        producer.write_text("#!/bin/bash\nif [ \"${1:-}\" = \"--paths\" ]; then\n"
                            "  printf '%s\\n' RESULTS_INDEX.md results_index.csv ab_test_runtime/audit/artifact_structural_audit.json ab_test_runtime/audit/goal_evidence_audit.json\nfi\n")
        self.git(root, "add", "tools/regen_derived.sh", "ab_test_runtime/audit/artifact_structural_audit.json")
        self.git(root, "commit", "-qm", "audit fixture base")
        return helper, audit

    def test_registered_audit_output_is_allowed_but_foreign_directory_is_not(self):
        import tempfile
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper, audit = self.make_audit_repo(root)
                before = self.git(root, "rev-parse", "HEAD")
                (root / "RESULTS_INDEX.md").write_text("new index\n")
                target = (root / "ab_test_runtime" / "audit_extra") if foreign else audit
                target.mkdir(exist_ok=True)
                new = target / "goal_evidence_audit.json"
                new.write_text('{"new": true}\n')
                self.git(root, "add", str(new.relative_to(root)))
                result = subprocess.run([get_bash_executable(), str(helper), "fixture"], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                if foreign:
                    self.assertEqual(before, self.git(root, "rev-parse", "HEAD"))
                    self.assertIn("other changes are staged", result.stderr)
                    self.assertEqual(new.relative_to(root).as_posix(), self.git(root, "diff", "--cached", "--name-only"))
                else:
                    self.assertNotEqual(before, self.git(root, "rev-parse", "HEAD"))
                    self.assertEqual('{"new": true}', self.git(root, "show", "HEAD:ab_test_runtime/audit/goal_evidence_audit.json"))
                    self.assertEqual("new index", self.git(root, "show", "HEAD:RESULTS_INDEX.md"))

    def test_new_untracked_and_already_staged_derived_outputs_trigger_real_commit(self):
        import tempfile
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper, audit = self.make_audit_repo(root)
                before = self.git(root, "rev-parse", "HEAD")
                new = audit / "goal_evidence_audit.json"
                new.write_text('{"new": true}\n')
                if staged:
                    self.git(root, "add", str(new.relative_to(root)))
                self.assertEqual("", self.git(root, "diff", "--name-only"))
                result = subprocess.run([get_bash_executable(), str(helper), "fixture"], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotEqual(before, self.git(root, "rev-parse", "HEAD"))
                self.assertEqual(before, self.git(root, "rev-parse", "HEAD^"))
                self.assertEqual('{"new": true}', self.git(root, "show", "HEAD:ab_test_runtime/audit/goal_evidence_audit.json"))
                self.assertEqual("", self.git(root, "diff", "--cached", "--name-only"))

    def test_clean_derived_state_does_not_commit_unrelated_working_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, audit = self.make_audit_repo(root)
            before = self.git(root, "rev-parse", "HEAD")
            (root / "unrelated_RESULTS_INDEX.md").write_text("foreign new\n")
            result = subprocess.run([get_bash_executable(), str(helper), "fixture"], cwd=root,
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
                (audit / "goal_evidence_audit.json").write_text('{"new": true}\n')
                self.git(root, "add", "ab_test_runtime/audit/goal_evidence_audit.json")
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
                environment.update(HOOK_TEST_GIT=shutil.which("git"), HOOK_TEST_COMMANDS=str(commands),
                                   HOOK_TEST_FAIL=command)
                result = run_with_fake_git(executable.parent,
                                           "bash %s fixture" % bash_path(helper),
                                           root, environment)
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
                result = subprocess.run([get_bash_executable(), str(repo / "ready.sh")], cwd=repo, capture_output=True, text=True)
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
                result = subprocess.run([get_bash_executable(), str(repo / "ready.sh")], cwd=repo, capture_output=True, text=True)
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
            result = run_with_fake_git(bin_dir, "bash %s" % bash_path(repo / "ready.sh"),
                                       repo, os.environ.copy())
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("cannot inspect", result.stderr)
            self.assertFalse((repo / "verifier-ran").exists())


class ActualDerivedPathSafetyTests(unittest.TestCase):
    def test_real_helpers_publish_only_registered_outputs_and_refuse_foreign_staging(self):
        import shutil
        import tempfile
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                def git(*args):
                    return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
                git('init','-q');git('config','user.name','Fixture')
                git('config','user.email','fixture@example.invalid');git('config','core.hooksPath','/dev/null')
                tools=root/'tools';tools.mkdir()
                for name in ('regen_derived.sh','regen_derived_commit.sh'):
                    shutil.copy2(REPO/'tools'/name,tools/name)
                outputs=('RESULTS_INDEX.md','results_index.csv','LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md',
                         'ab_test_runtime/audit/artifact_structural_audit.json',
                         'ab_test_runtime/audit/legacy_attribution_audit.json',
                         'app/tests/unit_test_inventory.json','GOALS.md')
                for name in outputs:
                    target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('old fixture')
                local=root/'ab_test_runtime/audit/machine_local.json';local.write_text('original local')
                python=root/'app/env/bin/python';python.parent.mkdir(parents=True)
                python.write_text('#!/bin/sh\nprintf "rebuilt" > "$FIXTURE_ROOT/RESULTS_INDEX.md"\nprintf "generated" > "$FIXTURE_ROOT/ab_test_runtime/audit/goal_evidence_audit.json"\n')
                python.chmod(0o755)
                git('add','.');git('commit','-qm','fixture base');before=git('rev-parse','HEAD')
                local.write_text('edited local')
                untracked=local.with_name('private_machine.json');untracked.write_text('untracked local')
                if staged:git('add',str(untracked.relative_to(root)))
                result=subprocess.run([get_bash_executable(),str(tools/'regen_derived_commit.sh'),'fixture'],cwd=root,
                                      env=test_derived_metadata.fixture_environment({'FIXTURE_ROOT':str(root)}),capture_output=True,text=True,timeout=10)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                if staged:
                    self.assertEqual(before,git('rev-parse','HEAD'))
                    self.assertIn('other changes are staged',result.stderr)
                    self.assertEqual(untracked.relative_to(root).as_posix(),git('diff','--cached','--name-only'))
                else:
                    self.assertNotEqual(before,git('rev-parse','HEAD'))
                    self.assertEqual('rebuilt',git('show','HEAD:RESULTS_INDEX.md'))
                    self.assertEqual('generated',git('show','HEAD:ab_test_runtime/audit/goal_evidence_audit.json'))
                    self.assertEqual('original local',git('show','HEAD:ab_test_runtime/audit/machine_local.json'))
                    self.assertNotIn(str(untracked.relative_to(root)),git('ls-tree','-r','--name-only','HEAD'))
                    self.assertEqual('',git('diff','--cached','--name-only'))
                self.assertEqual('edited local',local.read_text())
                self.assertEqual('untracked local',untracked.read_text())


if __name__ == "__main__":
    unittest.main()
