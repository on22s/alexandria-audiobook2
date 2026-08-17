"""Behavioural tests for gpu_job.sh, the thing that stops two jobs sharing a GPU.

WHY THESE EXIST. The lock has failed twice in ways that cost real time, and
both times it was verified by hand afterwards rather than by a test:

  - the script called `flock 9` and never checked it, so a failed acquisition
    fell straight through to running the command. `set -e` is deliberately off
    (the wrapped command's exit code has to survive), which is exactly what
    made the unchecked call dangerous.
  - the fixed version was committed locally and the CLOUD BOX KEPT RUNNING THE
    OLD ONE for hours, unnoticed, because nothing compared them.

Manual verification catches a bug once. This catches it every run, on whichever
machine runs the suite.

Each test drives the real script in a temporary directory with its own lock and
queue log, so nothing here touches the actual GPU lock.
"""
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GPU_JOB = os.path.join(REPO, "gpu_job.sh")


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class GpuJobTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self.tmp.name, "gpu.lock")
        self.qlog = os.path.join(self.tmp.name, "queue.log")

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, *argv, path_prefix=None, lock=None, timeout=30,
                allow_dirty="1"):
        # These jobs are `true`/`false` probes, not experiments - nothing here
        # produces an artifact anyone will cite, so the dirty-tree gate is
        # legitimately waived. It is exercised on purpose in
        # DirtyTreeGateTest below rather than being disabled and forgotten.
        env = dict(os.environ, GPU_LOCK=lock or self.lock, GPU_QLOG=self.qlog,
                   ALLOW_DIRTY_TREE=allow_dirty)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env["PATH"]
        return subprocess.run(["bash", GPU_JOB, *argv], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def log(self):
        if not os.path.exists(self.qlog):
            return ""
        with open(self.qlog, encoding="utf-8") as fh:
            return fh.read()

    def fake_bin(self, name, body):
        """A directory holding one executable that shadows the real one."""
        d = os.path.join(self.tmp.name, "bin_" + name)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(p, 0o755)
        return d

    # ---------------------------------------------------------------- basics

    def test_a_successful_job_runs_and_logs_ok(self):
        marker = os.path.join(self.tmp.name, "ran")
        r = self.run_job("good", "touch", marker)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(marker))
        self.assertIn("START    good", self.log())
        self.assertIn("OK       good", self.log())

    def test_queue_log_order_is_queued_then_ident_then_start_then_result(self):
        # IDENT sits between QUEUED and START on purpose: it must describe the
        # code that is ABOUT to run. Recorded after the fact it would be a
        # post-mortem, which is what reading logs already gave us.
        self.run_job("ordered", "true")
        lines = [l.split()[1] for l in self.log().splitlines() if l.strip()]
        lines = [l for l in lines if l != "DIRTY_RUN"]
        self.assertEqual(lines, ["QUEUED", "IDENT", "START", "OK"])

    # ------------------------------------------------------- failure surfaces

    def test_wrapped_failure_propagates_the_exit_code(self):
        """A chained job that fails quietly gets read as a result."""
        r = self.run_job("bad", "bash", "-c", "exit 37")
        self.assertEqual(r.returncode, 37)
        self.assertIn("FAILED   bad rc=37", self.log())
        self.assertNotIn("OK ", self.log())

    def test_failure_is_announced_on_stderr(self):
        r = self.run_job("bad", "bash", "-c", "exit 1")
        self.assertIn("FAILED", r.stderr)

    @unittest.skipUnless(shutil.which("timeout"), "timeout command unavailable")
    def test_timeout_kills_job_releases_lock_and_logs_failure(self):
        """A timed-out GPU job must not poison the queue behind it."""
        r = self.run_job("timed", "timeout", "0.2", "sleep", "10")
        self.assertEqual(124, r.returncode)
        self.assertIn("FAILED   timed rc=124", self.log())
        next_job = self.run_job("after_timeout", "true")
        self.assertEqual(0, next_job.returncode)
        self.assertIn("OK       after_timeout", self.log())

    # ------------------------------------------------------------ the gate

    def test_a_failed_flock_refuses_to_run_the_command(self):
        """THE DEFECT. An unchecked `flock` ran the command anyway."""
        marker = os.path.join(self.tmp.name, "must_not_exist")
        d = self.fake_bin("flock", "#!/bin/bash\nexit 73\n")
        r = self.run_job("gated", "touch", marker, path_prefix=d)
        self.assertFalse(os.path.exists(marker),
                         "command ran despite the lock failing")
        self.assertEqual(r.returncode, 4)
        self.assertIn("LOCK_FAILED", self.log())
        self.assertNotIn("START", self.log())
        self.assertNotIn("OK", self.log())

    def test_an_unopenable_lock_file_refuses_to_run(self):
        marker = os.path.join(self.tmp.name, "must_not_exist")
        bad = os.path.join(self.tmp.name, "no_such_dir", "gpu.lock")
        r = self.run_job("nolock", "touch", marker, lock=bad)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(r.returncode, 4)
        self.assertIn("LOCK_FAILED", self.log())

    # --------------------------------------------------------- serialisation

    def test_two_jobs_do_not_overlap(self):
        """The whole point. Overlap cost 42 minutes of A6000 training once.

        Each job appends to a witness file on entry and exit; interleaved
        markers would mean both held the GPU at once.
        """
        witness = os.path.join(self.tmp.name, "witness")
        script = textwrap.dedent(f"""
            echo in >> {witness}
            sleep 1
            echo out >> {witness}
        """)
        env = dict(os.environ, GPU_LOCK=self.lock, GPU_QLOG=self.qlog,
                   ALLOW_DIRTY_TREE="1")
        procs = [subprocess.Popen(["bash", GPU_JOB, f"j{i}", "bash", "-c",
                                   script], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
                 for i in range(2)]
        for p in procs:
            p.wait(timeout=60)
        with open(witness, encoding="utf-8") as fh:
            seq = [l.strip() for l in fh if l.strip()]
        self.assertEqual(seq, ["in", "out", "in", "out"],
                         f"jobs overlapped: {seq}")

    def test_the_second_job_waits_rather_than_failing(self):
        """Blocking is correct; a queued job must not be dropped."""
        env = dict(os.environ, GPU_LOCK=self.lock, GPU_QLOG=self.qlog,
                   ALLOW_DIRTY_TREE="1")
        slow = subprocess.Popen(["bash", GPU_JOB, "slow", "sleep", "2"],
                                env=env, stdout=subprocess.DEVNULL)
        time.sleep(0.4)
        r = self.run_job("waiter", "true", timeout=60)
        slow.wait(timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("OK       waiter", self.log())

    def test_interrupting_a_waiter_releases_nothing_and_logs_no_start(self):
        """A job killed while queued must not appear to have run."""
        env = dict(os.environ, GPU_LOCK=self.lock, GPU_QLOG=self.qlog,
                   ALLOW_DIRTY_TREE="1")
        holder = subprocess.Popen(["bash", GPU_JOB, "holder", "sleep", "3"],
                                  env=env, stdout=subprocess.DEVNULL)
        time.sleep(0.4)
        marker = os.path.join(self.tmp.name, "waiter_ran")
        waiter = subprocess.Popen(["bash", GPU_JOB, "waiter", "touch", marker],
                                  env=env, stdout=subprocess.DEVNULL)
        time.sleep(0.4)
        waiter.kill()
        waiter.wait(timeout=30)
        holder.wait(timeout=60)
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("START    waiter", self.log())
        self.assertIn("OK       holder", self.log())

    # -------------------------------------------------- deployment identity

    def test_identity_is_logged_before_start(self):
        """Written BEFORE the job runs, or it is a post-mortem, not a record.

        Two jobs died on 2026-08-04 because a box was running a superseded copy
        of this script. Nothing announced it; it was found by reading logs
        afterwards.
        """
        self.run_job("ident", "true")
        # DIRTY_RUN appears only because these probe runs waive the dirty-tree
        # gate (see run_job); it is not part of the lifecycle under test.
        kinds = [l.split()[1] for l in self.log().splitlines() if l.strip()
                 and l.split()[1] != "DIRTY_RUN"]
        self.assertEqual(kinds, ["QUEUED", "IDENT", "START", "OK"])

    def test_identity_carries_what_is_needed_to_tell_two_runs_apart(self):
        self.run_job("ident", "echo", "hello")
        line = [l for l in self.log().splitlines() if " IDENT " in l][0]
        for field in ("commit=", "tree=", "gpu_job_sha=", "host=", "gpu=",
                      "cmd="):
            self.assertIn(field, line)
        self.assertIn("echo hello", line)

    def test_the_script_hash_actually_identifies_the_script(self):
        """The field that would have caught the stale cloud copy."""
        self.run_job("ident", "true")
        line = [l for l in self.log().splitlines() if " IDENT " in l][0]
        logged = line.split("gpu_job_sha=")[1].split()[0]
        import hashlib
        with open(GPU_JOB, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()[:12]
        self.assertEqual(logged, actual)

    def test_identity_degrades_rather_than_blocking_the_job(self):
        """Identity is evidence, not a gate.

        With git and both smi tools absent, the job must still run - a
        provenance record that can refuse to start work is worse than none.
        """
        d = self.tmp.name + "/emptybin"
        os.makedirs(d, exist_ok=True)
        for tool in ("git", "nvidia-smi", "rocm-smi", "sha256sum", "hostname"):
            p = os.path.join(d, tool)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/bash\nexit 127\n")
            os.chmod(p, 0o755)
        marker = os.path.join(self.tmp.name, "ran_anyway")
        r = self.run_job("degraded", "touch", marker, path_prefix=d)
        self.assertTrue(os.path.exists(marker), "job blocked by identity capture")
        self.assertEqual(r.returncode, 0)
        self.assertIn("IDENT", self.log())
        self.assertIn("unknown", self.log())

    # ------------------------------------------------------------- misuse

    def test_no_command_is_rejected(self):
        self.assertEqual(self.run_job("nameonly").returncode, 2)

    def test_no_name_is_rejected(self):
        r = subprocess.run(["bash", GPU_JOB], capture_output=True, text=True,
                           env=dict(os.environ, GPU_LOCK=self.lock,
                                    GPU_QLOG=self.qlog,
                                    ALLOW_DIRTY_TREE="1"))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class DirtyTreeGateTest(unittest.TestCase):
    """Evidence from uncommitted code is not reproducible.

    86 of 178 recorded runs - 48% - produced artifacts from a dirty tree while
    gpu_job.sh dutifully wrote `tree=dirty` and nothing read it.
    `respelling_rule_b.json` was one: the artifact was committed, its source
    never was.

    The script resolves git state from its OWN directory, so each case here
    copies it into a throwaway repository rather than depending on the state of
    the checkout the suite happens to run in.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.script = os.path.join(self.root, "gpu_job.sh")
        shutil.copy(GPU_JOB, self.script)
        os.chmod(self.script, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", self.root, *args], check=True,
                       capture_output=True)

    def _make_repo(self, dirty):
        os.makedirs(os.path.join(self.root, "app", "experiments"), exist_ok=True)
        with open(os.path.join(self.root, "app", "experiments", "kept.py"), "w") as h:
            h.write("TRACKED = True\n")
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        self._git("add", "gpu_job.sh", "app/experiments/kept.py")
        self._git("commit", "-qm", "baseline")
        if dirty:
            with open(os.path.join(self.root, "gpu_job.sh"), "a") as handle:
                handle.write("\n# an uncommitted change\n")

    def _add_untracked(self, name):
        path = os.path.join(self.root, "app", "experiments", name)
        with open(path, "w") as handle:
            handle.write("# brand new, not yet committed\n")
        return path

    def _run(self, allow_dirty=None):
        env = dict(os.environ,
                   GPU_LOCK=os.path.join(self.root, "gpu.lock"),
                   GPU_QLOG=os.path.join(self.root, "queue.log"))
        env.pop("ALLOW_DIRTY_TREE", None)
        if allow_dirty is not None:
            env["ALLOW_DIRTY_TREE"] = allow_dirty
        return subprocess.run(["bash", self.script, "probe", "true"],
                              env=env, capture_output=True, text=True, timeout=30)

    def _log(self):
        path = os.path.join(self.root, "queue.log")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""

    def test_a_dirty_tree_is_refused_and_the_command_never_runs(self):
        self._make_repo(dirty=True)
        result = self._run()
        self.assertEqual(5, result.returncode)
        self.assertIn("refusing to run", result.stderr)
        self.assertIn("REFUSED", self._log())
        self.assertNotIn("START", self._log())

    def test_the_refusal_names_what_is_uncommitted(self):
        # A gate that says only "no" gets overridden reflexively.
        self._make_repo(dirty=True)
        self.assertIn("gpu_job.sh", self._run().stderr)

    def test_the_override_runs_but_leaves_a_mark(self):
        self._make_repo(dirty=True)
        result = self._run(allow_dirty="1")
        self.assertEqual(0, result.returncode)
        self.assertIn("WARNING", result.stderr)
        log = self._log()
        self.assertIn("DIRTY_RUN", log)
        # The provenance line must still say dirty, so an overridden run is
        # never mistaken for a clean one afterwards.
        self.assertIn("tree=dirty:", log)

    def test_a_clean_tree_runs_untouched(self):
        self._make_repo(dirty=False)
        self.assertEqual(0, self._run().returncode)
        self.assertIn("tree=clean", self._log())
        self.assertNotIn("REFUSED", self._log())

    def test_a_directory_that_is_not_a_repository_is_not_called_dirty(self):
        # "Cannot tell" must not be spelled "dirty": an exported tree or a
        # container without git has nothing to commit and must still run.
        result = self._run()
        self.assertEqual(0, result.returncode)
        self.assertIn("tree=unknown", self._log())
        self.assertNotIn("REFUSED", self._log())

    def test_an_untracked_experiment_script_is_refused(self):
        """The case `git diff HEAD` cannot see.

        A new experiment script is untracked for exactly as long as it takes
        to write and run it, which is when it produces its artifact.
        trim_silence_build.py produced goal 5.4's alignment result that way.
        """
        self._make_repo(dirty=False)
        self._add_untracked("brand_new_probe.py")
        result = self._run()
        self.assertEqual(5, result.returncode)
        self.assertIn("REFUSED", self._log())
        self.assertNotIn("START", self._log())

    def test_untracked_notes_are_not_treated_as_dirt(self):
        """Counting scratch files made an earlier dirty flag true on every
        run, which is the same as being false."""
        self._make_repo(dirty=False)
        self._add_untracked("NOTES.md")
        with open(os.path.join(self.root, "scratch.txt"), "w") as handle:
            handle.write("thinking out loud\n")
        self.assertEqual(0, self._run().returncode)
        self.assertIn("tree=clean", self._log())

    def test_the_shell_gate_agrees_with_the_python_provenance(self):
        """Two implementations of one question WILL drift (Rule 15).

        gpu_job.sh cannot import Python - it is the lock wrapper and has to
        work when the venv does not - so the definition exists twice on
        purpose. This is the thing that notices when they stop matching, run
        against whatever state this checkout happens to be in.
        """
        from experiments.manifest import _git_state

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        python_says_dirty = _git_state(repo_root)["dirty"]

        state = subprocess.run(
            ["bash", "-c",
             f'source <(sed -n "/^tree_state()/,/^}}/p" {GPU_JOB!r}); '
             f'cd {repo_root!r} && set -- {GPU_JOB!r} && tree_state'],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if state == "unknown":
            self.skipTest("git could not answer in this environment")
        shell_says_dirty = state.startswith("dirty")

        self.assertEqual(
            python_says_dirty, shell_says_dirty,
            f"manifest._git_state says dirty={python_says_dirty} but "
            f"gpu_job.sh tree_state says {state!r}; the gate and the "
            "provenance stamp must not disagree about the same tree")


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class LlmPreflightGateTest(unittest.TestCase):
    """The lock cannot tell you there was no engine.

    The PR #308 remeasurement ran with nothing on port 8090, recorded rc=1 and
    wrote an empty results list - which reads as "the experiment failed"
    rather than "there was nothing to talk to", and stayed undiagnosed for a
    day. gpu_job.sh serialises the card and propagates exit codes; it has no
    idea whether a server exists. Hence an opt-in check.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qlog = os.path.join(self.tmp.name, "queue.log")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **extra):
        env = dict(os.environ,
                   GPU_LOCK=os.path.join(self.tmp.name, "gpu.lock"),
                   GPU_QLOG=self.qlog, ALLOW_DIRTY_TREE="1", **extra)
        return subprocess.run(["bash", GPU_JOB, "probe", "true"],
                              env=env, capture_output=True, text=True, timeout=60)

    def _log(self):
        return open(self.qlog, encoding="utf-8").read() if os.path.exists(self.qlog) else ""

    def test_no_check_happens_unless_asked(self):
        # Most jobs here are TTS and need no language model.
        self.assertEqual(0, self._run().returncode)
        self.assertNotIn("NO_LLM", self._log())
        self.assertNotIn("LLM_UNCHECKED", self._log())

    def test_a_failing_preflight_stops_the_job_before_it_starts(self):
        result = self._run(REQUIRE_LLM="1", LLM_PREFLIGHT_PYTHON="/bin/false")
        self.assertEqual(6, result.returncode)
        self.assertIn("NO_LLM", self._log())
        self.assertNotIn("START", self._log())

    def test_an_unavailable_checker_warns_rather_than_blocking(self):
        # "Cannot check" is not "failed": missing the checker must not stop a
        # run, the same third answer tree_state gives for a non-repo.
        result = self._run(REQUIRE_LLM="1",
                           LLM_PREFLIGHT_PYTHON="/nonexistent/python")
        self.assertEqual(0, result.returncode)
        self.assertIn("LLM_UNCHECKED", self._log())
        self.assertIn("START", self._log())

    def test_the_gate_does_not_read_the_ambient_PYTHON_variable(self):
        """Pinokio exports PYTHON=<miniforge>/python, which does not exist.

        `${PYTHON:-default}` therefore takes the broken value - the variable
        IS set, so the default never fires - and the first version of this
        gate silently downgraded to "unchecked" on its first real run.
        """
        result = self._run(REQUIRE_LLM="1", PYTHON="/nonexistent/python",
                           LLM_PREFLIGHT_PYTHON="/bin/false")
        self.assertEqual(6, result.returncode, "PYTHON must not be consulted")
        self.assertIn("NO_LLM", self._log())
