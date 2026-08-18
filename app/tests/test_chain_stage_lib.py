"""The shared stage runner: chains must fail when their stages fail.

21 of 30 chains captured a per-item exit code, printed it, and never looked
again - so the re-gate printed COMPLETE and exited 0 while all 67 of its
adapters failed, and two GPU hours that measured nothing were logged as OK.
Bash discards a loop iteration's status and `set -e` does not reach inside a
loop body, so this is the language behaving as documented, not a surprise.

These tests drive the real lib/stage.sh through bash rather than reimplementing
its logic, because a reimplementation is the thing that drifts.
"""
import os
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(REPO, "run_chains", "lib", "stage.sh")


@unittest.skipUnless(os.path.exists(LIB), "run_chains/lib/stage.sh not present")
class StageRunnerTest(unittest.TestCase):
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
                STAGE_LOG_DIR="{self.tmp.name}/logs"
                source "{LIB}"
                {textwrap.dedent(body)}
                """))
        os.chmod(script, 0o755)
        return subprocess.run(["bash", script], capture_output=True, text=True,
                              timeout=60)

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

    def test_running_the_same_chain_twice_is_idempotent(self):
        """Best practice from every pipeline guide: prove it by running twice.

        Our chains claimed skip-if-exists and the n1200 block proved the claim
        wrong - it skipped on EXISTENCE, so a run truncated at 1129 of 1200
        terms would have been treated as done forever.
        """
        marker = os.path.join(self.tmp.name, "artifact")
        body = f'''
            if [ -e "{marker}" ]; then
                stage_note "SKIP work (artifact exists)"
            else
                run_stage work 10s -- touch "{marker}"
            fi
            stage_summary demo
        '''
        first = self._chain(body)
        second = self._chain(body)
        self.assertIn("OK    work", first.stdout)
        self.assertIn("SKIP work (artifact exists)", second.stdout)
        self.assertNotIn("START work", second.stdout)
        self.assertEqual(0, second.returncode)


if __name__ == "__main__":
    unittest.main()
