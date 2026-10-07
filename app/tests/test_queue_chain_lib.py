"""The shared queue helper, and the guards the committed chains must keep.

Six one-off waiters were written outside the repository in a single day, each
hardcoding a PID because the chain it waited for was itself outside
`run_chains/` and could not be matched by name. A PID cannot be re-run
tomorrow and cannot be committed as the record of how a result was produced,
which is why the chains belong here and the waiting is by name.

Three behaviours are pinned because each has already failed in this repo.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
LIB = REPO / "run_chains" / "lib" / "queue.sh"
CHAINS = [REPO / "run_chains" / "anchor_and_separator_table_20260826.sh",
          REPO / "run_chains" / "resume_partial_arm_20260826.sh"]


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.src = LIB.read_text(encoding="utf-8")

    def test_it_excludes_itself_from_its_own_process_match(self):
        # Native identity and decoy cases are exercised by QueueProcessTests.
        self.assertNotIn("pgrep -f", self.src[self.src.index('chain_running() {'):])
        for owner in ("$$", "$PPID", "$BASHPID"):
            self.assertIn(owner, self.src[self.src.index('chain_running() {'):])

    def test_it_can_wait_for_a_chain_that_has_not_started(self):
        """Waiting for a chain that is not running YET returns immediately and
        the waiter takes the card out from under work about to begin."""
        self.assertIn("grace", self.src)
        self.assertIn("to appear", self.src)

    def test_the_interpreter_resolver_lives_here_only(self):
        self.assertIn("resolve_python", self.src)
        self.assertIn("main_checkout", self.src)

    def test_dirty_tree_preflight_uses_canonical_source_policy(self):
        self.assertIn("--print-source-state", self.src)
        self.assertNotIn("git -C \"$repo\" status", self.src)



class ChainTest(unittest.TestCase):
    def test_every_committed_chain_parses(self):
        for chain in CHAINS:
            with self.subTest(chain=chain.name):
                out = subprocess.run(["bash", "-n", str(chain)],
                                     capture_output=True, text=True)
                self.assertEqual(0, out.returncode, out.stderr)

    def test_every_chain_refuses_a_dirty_tree_before_doing_work(self):
        """22 refusals in one day, all reading `uncommitted changes`. A chain
        launched from a dirty tree reaches its summary in minutes having run
        nothing."""
        for chain in CHAINS:
            with self.subTest(chain=chain.name):
                src = chain.read_text(encoding="utf-8")
                self.assertIn("refuse_if_dirty", src)
                self.assertLess(src.index("refuse_if_dirty"),
                                src.index("run_stage"),
                                "the refusal must come before any stage")

    def test_every_chain_finds_the_interpreter_from_a_worktree(self):
        """The venv lives in the main checkout; a worktree has none."""
        for chain in CHAINS:
            with self.subTest(chain=chain.name):
                src = chain.read_text(encoding="utf-8")
                self.assertIn("resolve_python", src,
                              "must use the shared resolver, not its own copy")
                self.assertNotIn("main_checkout", src,
                                 "a second copy of the fallback will drift")

    def test_the_resume_chain_treats_cannot_tell_as_a_refusal(self):
        """COMPLETE / INCOMPLETE / CANNOT TELL are three answers. Collapsing
        the third into "incomplete" started a five-hour re-run of an arm that
        was already finished, because the interpreter was missing."""
        src = CHAINS[1].read_text(encoding="utf-8")
        self.assertIn("REFUSING: could not determine", src)
        # the refusal branch must exit, not fall through to the work
        start = src.index("case \"$state\"")
        branch = src[start:src.index("esac", start)]
        self.assertIn("exit 1", branch)

    def test_the_table_chain_refuses_a_partial_arm(self):
        """A partial arm does not make the table smaller, it makes it wrong:
        terms are ordered by book count, so a truncated arm is the commonest
        words only."""
        src = CHAINS[0].read_text(encoding="utf-8")
        self.assertIn("the dot arm is not complete", src)
        self.assertLess(src.index('respelling_completion.py" "$dot" 1600'),
                        src.index("pauses_four_arms 1h"))


class PredecessorWaitTests(unittest.TestCase):
    def run_wait(self, body):
        return subprocess.run(["bash", "-c", 'set -uo pipefail\nsource "$1"\n'+body,
                               "fixture", str(LIB)], capture_output=True, text=True, timeout=5)

    def test_actual_caller_loops_refuse_inspection_errors_before_following_stage(self):
        for name in ("narrator_x_context_20260821.sh", "anchor_and_separator_table_20260826.sh"):
            source=(REPO/"run_chains"/name).read_text()
            loop=re.search(r"for chain in .*?\ndone", source, re.S).group()
            with self.subTest(name=name, inspection="error"):
                result=self.run_wait('chain_running() { echo "inspection refused" >&2; return 2; }\n'+
                                     loop+'\nprintf "FOLLOWING_STAGE_REACHED\\n"\n')
                self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertIn("inspection refused",result.stderr)
                self.assertNotIn("FOLLOWING_STAGE_REACHED",result.stdout)
            with self.subTest(name=name, inspection="stopped"):
                result=self.run_wait('chain_running() { return 1; }\n'+loop+
                                     '\nprintf "FOLLOWING_STAGE_REACHED\\n"\n')
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertIn("FOLLOWING_STAGE_REACHED",result.stdout)

    def test_running_predecessor_finishes_and_seven_second_grace_keeps_exact_delays(self):
        result=self.run_wait('''calls=0
chain_running() { calls=$((calls+1)); [ "$calls" -lt 3 ]; }
sleep() { printf "SLEEP=%s\\n" "$1"; }
wait_for_chain fixture.sh || exit 1
chain_running() { return 1; }
wait_for_chain fixture.sh 7
''')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn("fixture.sh finished",result.stdout)
        self.assertEqual(["5", "5", "2"], re.findall(r"SLEEP=(\d+)",result.stdout))


if __name__ == "__main__":
    unittest.main()
