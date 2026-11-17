"""The shared stage runner: chains must fail when their stages fail.

21 of 30 chains captured a per-item exit code, printed it, and never looked
again - so the re-gate printed COMPLETE and exited 0 while all 67 of its
adapters failed, and two GPU hours that measured nothing were logged as OK.
Bash discards a loop iteration's status and `set -e` does not reach inside a
loop body, so this is the language behaving as documented, not a surprise.

These tests drive the real lib/stage.sh through bash rather than reimplementing
its logic, because a reimplementation is the thing that drifts.
"""
from tests.test_support import get_bash_path, run_bash_fixture, write_bash_fixture
from tests.test_support import get_isolated_git_environment
import os
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(REPO, "run_chains", "lib", "stage.sh")


@unittest.skipUnless(os.path.exists(LIB), "run_chains/lib/stage.sh not present")
class StageRunnerTest(unittest.TestCase):
    def test_missing_dependency_name_refuses_without_loop_or_worker_in_plain_bash(self):
        for cached in (False, True):
            for value in ("", " --", " --needs-vram"):
                with self.subTest(cached=cached, value=value):
                    worker = os.path.join(self.tmp.name, "worker")
                    checker = os.path.join(self.tmp.name, "checker")
                    invocation = (f'run_validated_cached_stage probe 2s touch "{checker}" --'
                                  if cached else 'run_stage probe 2s')
                    script = f'source "{get_bash_path(LIB)}"; STAGE_LOG_DIR="{get_bash_path(self.tmp.name)}/logs"; {invocation} --requires-ok{value}'
                    try:
                        result = run_bash_fixture(["-c", script], capture_output=True,
                                                text=True, timeout=1, env=get_isolated_git_environment())
                    except subprocess.TimeoutExpired:
                        self.fail("Malformed --requires-ok looped instead of refusing")
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("requires-ok", result.stdout + result.stderr)
                    self.assertFalse(os.path.exists(worker))
                    self.assertFalse(os.path.exists(checker))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _chain(self, body):
        """Run a miniature chain that sources the real library."""
        script = os.path.join(self.tmp.name, "chain.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/usr/bin/bash
                set -uo pipefail
                STAGE_LOG_DIR="{get_bash_path(self.tmp.name)}/logs"
                source "{get_bash_path(LIB)}"
                {textwrap.dedent(body)}
                """))
        os.chmod(script, 0o755)
        return run_bash_fixture([script], capture_output=True, text=True,
                              timeout=60, env=get_isolated_git_environment())

    def test_a_failing_stage_makes_the_chain_exit_non_zero(self):
        """THE DEFECT: this used to exit 0 and be logged as OK."""
        r = self._chain('''
            run_stage good 10s -- true
            run_stage bad 10s -- bash -c "exit 2"
            stage_summary demo
        ''')
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("FAIL  bad rc=2", r.stdout)
        self.assertIn("bad = failed:2", r.stdout)

    def test_all_stages_passing_exits_zero(self):
        r = self._chain('''
            run_stage a 10s -- true
            run_stage b 10s -- true
            stage_summary demo
        ''')
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("2/2 stages ok", r.stdout)

    def test_a_later_stage_still_runs_after_an_unrelated_failure(self):
        """Continue-on-failure is deliberate: one dead stage at 2am must not
        take the remaining ten hours with it. The chain still exits non-zero."""
        r = self._chain('''
            run_stage bad 10s -- bash -c "exit 1"
            run_stage later 10s -- true
            stage_summary demo
        ''')
        self.assertIn("OK    later", r.stdout)
        self.assertEqual(1, r.returncode)

    def test_requires_ok_skips_a_stage_whose_predecessor_failed(self):
        """task-spooler's -W: run after it ends WELL, not merely ends.

        unseen_books started after a stage that had killed the server it
        needed, and aborted instantly.
        """
        r = self._chain('''
            run_stage server 10s -- bash -c "exit 1"
            run_stage needs_server 10s --requires-ok server -- true
            stage_summary demo
        ''')
        self.assertIn("SKIP  needs_server (requires server, which is failed:1)",
                      r.stdout)

    def test_requires_ok_treats_a_stage_that_never_ran_as_unsatisfied(self):
        # Resumed chains are the normal case here; "I did not see it succeed"
        # must not be read as "it succeeded".
        r = self._chain('''
            run_stage needs_missing 10s --requires-ok never_ran -- true
            stage_summary demo
        ''')
        self.assertIn("which is missing", r.stdout)

    def test_a_timeout_is_reported_as_a_timeout_not_a_generic_failure(self):
        """rc=124 means the cap was too small, which is a different problem
        from the job failing - and it is what truncated the n1200 block."""
        r = self._chain('''
            run_stage slow 1s -- sleep 30
            stage_summary demo
        ''')
        self.assertIn("TIMEOUT slow", r.stdout)
        self.assertEqual(1, r.returncode)

    def test_each_stage_writes_its_own_log(self):
        self._chain('''
            run_stage talky 10s -- bash -c "echo hello from the stage"
            stage_summary demo
        ''')
        log = os.path.join(self.tmp.name, "logs", "talky.log")
        self.assertTrue(os.path.exists(log))
        with open(log, encoding="utf-8") as fh:
            self.assertIn("hello from the stage", fh.read())

    def test_artifact_commit_preserves_unrelated_staged_change(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, "ab_test_runtime", "experiments"))
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, env=get_isolated_git_environment())
        for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "-C", repo, "config", key, value], check=True, env=get_isolated_git_environment())
        artifact = os.path.join(repo, "ab_test_runtime", "experiments", "a.json")
        unrelated = os.path.join(repo, "unrelated.txt")
        for path in (artifact, unrelated):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("base\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, env=get_isolated_git_environment())
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "base"], check=True, env=get_isolated_git_environment())
        with open(artifact, "w", encoding="utf-8") as handle:
            handle.write("artifact\n")
        with open(unrelated, "w", encoding="utf-8") as handle:
            handle.write("unrelated\n")
        subprocess.run(["git", "-C", repo, "add", "unrelated.txt"], check=True, env=get_isolated_git_environment())
        command = (f'source "{get_bash_path(LIB)}"; STAGE_LOG_DIR="{get_bash_path(self.tmp.name)}/logs"; '
                   f'stage_commit_artifacts probe "{get_bash_path(repo)}" "ab_test_runtime/experiments/a.json"')
        result = run_bash_fixture(["-c", command], capture_output=True,
                                text=True, timeout=60, env=get_isolated_git_environment())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        committed = subprocess.run(
            ["git", "-C", repo, "show", "--pretty=", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True, env=get_isolated_git_environment()).stdout.splitlines()
        self.assertEqual(["ab_test_runtime/experiments/a.json"], committed)
        staged = subprocess.run(
            ["git", "-C", repo, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True, env=get_isolated_git_environment()).stdout.splitlines()
        self.assertEqual(["unrelated.txt"], staged)

    def test_running_the_same_chain_twice_is_idempotent(self):
        """Best practice from every pipeline guide: prove it by running twice.

        Our chains claimed skip-if-exists and the n1200 block proved the claim
        wrong - it skipped on EXISTENCE, so a run truncated at 1129 of 1200
        terms would have been treated as done forever.
        """
        marker = os.path.join(self.tmp.name, "artifact")
        body = f'''
            if [ -e "{get_bash_path(marker)}" ]; then
                stage_note "SKIP work (artifact exists)"
            else
                run_stage work 10s -- touch "{get_bash_path(marker)}"
            fi
            stage_summary demo
        '''
        first = self._chain(body)
        second = self._chain(body)
        self.assertIn("OK    work", first.stdout)
        self.assertIn("SKIP work (artifact exists)", second.stdout)
        self.assertNotIn("START work", second.stdout)
        self.assertEqual(0, second.returncode)




class StageDependencyAccountingTests(unittest.TestCase):
    def test_missing_failed_and_skipped_dependencies_make_summary_incomplete(self):
        import shlex
        from pathlib import Path
        for predecessor in (None, "failed:7", "skipped"):
            with self.subTest(predecessor=predecessor), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                initial = '' if predecessor is None else f'STAGE_RESULT[first]={shlex.quote(predecessor)}'
                script = f"""
set -uo pipefail
source {shlex.quote(get_bash_path(LIB))}
STAGE_LOG_DIR={shlex.quote(get_bash_path(root / 'logs'))}
{initial}
# Dependency failure must still precede server inspection and VRAM reclamation.
pgrep() {{ touch {shlex.quote(get_bash_path(root / 'unexpected_probe'))}; return 0; }}
pkill() {{ touch {shlex.quote(get_bash_path(root / 'unexpected_kill'))}; }}
run_stage child 2s --needs-vram --requires-ok first -- touch {shlex.quote(get_bash_path(root / 'child'))}
run_stage grandchild 2s --requires-ok child -- touch {shlex.quote(get_bash_path(root / 'grandchild'))}
run_stage independent 2s -- touch {shlex.quote(get_bash_path(root / 'independent'))}
printf '%s %s %s %s\\n' "$STAGE_TOTAL" "$STAGE_FAILURES" "${{STAGE_RESULT[child]}}" "${{STAGE_RESULT[grandchild]}}" > {shlex.quote(get_bash_path(root / 'state'))}
stage_summary dependency_probe
"""
                result = run_bash_fixture(["-c", script], capture_output=True, text=True, timeout=10, env=get_isolated_git_environment())
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertEqual("3 2 skipped skipped\n", (root / 'state').read_text())
                self.assertIn("1/3 stages ok", result.stdout)
                self.assertIn("SKIP  child", result.stdout)
                self.assertIn("SKIP  grandchild", result.stdout)
                self.assertTrue((root / 'independent').is_file())
                for name in ('child', 'grandchild', 'unexpected_probe', 'unexpected_kill'):
                    self.assertFalse((root / name).exists())
                self.assertEqual(['independent.log'], sorted(p.name for p in (root / 'logs').iterdir()))


class BashFixtureTransportTests(unittest.TestCase):
    def test_paths_and_arguments_round_trip_through_real_python_launcher(self):
        with tempfile.TemporaryDirectory(prefix="shell space '$ ") as directory:
            root = Path(directory) / '音声'
            root.mkdir()
            output = root / 'argv.json'
            launcher = root / 'worker'
            write_bash_fixture(launcher, '#!' + sys.executable + '\n'
                'import json,pathlib,sys\n'
                'pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]),encoding="utf-8")\n')
            arguments = ['space value', "apostrophe'value", '$not_expanded', 'semi;colon', '音声',
                         'double"quote', 'trailing\\', '', '--literal=/not/a/drive']
            result = run_bash_fixture([str(launcher), str(output), *arguments],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(arguments, json.loads(output.read_text(encoding='utf-8')))
            self.assertNotIn(b'\r\n', launcher.read_bytes())

    def test_caller_tool_precedes_system_tools_and_caller_git_is_not_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / 'sleep'
            write_bash_fixture(tool, '#!/bin/sh\nprintf fixture-tool\n')
            environment = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                               GIT_DIR='not-the-fixture', GIT_INDEX_FILE='not-the-index')
            result = run_bash_fixture(['-c', 'sleep 30; test -z "${GIT_DIR-}"; test -z "${GIT_INDEX_FILE-}"'],
                                      env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual('fixture-tool', result.stdout)

    def test_nested_bash_does_not_inherit_a_wrappers_errexit(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / 'wrapper'
            worker = Path(directory) / 'worker'
            write_bash_fixture(wrapper, '#!/bin/sh\nset -eu\nexec bash "$1"\n')
            write_bash_fixture(worker, '#!/bin/sh\nfalse\nprintf continued\n')
            result = run_bash_fixture([str(wrapper), str(worker)],
                                      capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual('continued', result.stdout)

    def test_shell_reads_existing_file_and_refuses_missing_file_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='source path ') as directory:
            existing = Path(directory) / 'source file'
            existing.write_text('known payload')
            source = 'cat -- ' + shlex.quote(get_bash_path(existing))
            result = run_bash_fixture(['-c', source], capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('known payload', result.stdout)
            existing.unlink()
            result = run_bash_fixture(['-c', source], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn('known payload', result.stdout)

    def test_native_resolver_rejects_missing_shell_instead_of_running_a_fallback(self):
        from tests.test_support import get_bash_executable
        get_bash_executable.cache_clear()
        self.addCleanup(get_bash_executable.cache_clear)
        with patch('tests.test_support.shutil.which', return_value=None), patch(
                'experiments.manifest._source_probe_shell', return_value=(None, 'no native shell')):
            with self.assertRaisesRegex(RuntimeError, 'no native shell'):
                get_bash_executable()


if __name__ == "__main__":
    unittest.main()
