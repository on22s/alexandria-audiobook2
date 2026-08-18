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
import shlex
import subprocess
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GPU_JOB = os.path.join(REPO, "gpu_job.sh")

# `unittest discover` is run from app/, which puts app/ on sys.path and makes
# `experiments` importable. Running this file directly puts app/tests/ there
# instead, and the Rule 15 cross-check below died on ModuleNotFoundError - a
# failure that only appeared once the stray mid-file unittest.main() stopped
# hiding four of the five classes from direct execution.
import sys
if os.path.join(REPO, "app") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "app"))


def isolated_env(tmpdir, **extra):
    """Every gpu_job.sh knob that otherwise points at REAL, SHARED state.

    GPU_PAUSE_FLAG belongs here for exactly the reason GPU_LOCK does, and was
    left out when the pause feature landed. The cost showed up the first time
    the queue was paused for real: gpu_job.sh waits on the flag BEFORE taking
    the lock (correctly - a paused job must not sit on the GPU), so every test
    that spawns a probe blocked on the USER's pause flag, 20 seconds at a
    time, until subprocess.run's 60s timeout turned it into an error. Twelve
    tests, and the release verifier with them, fail whenever someone pauses
    the queue - the one moment they are most likely to run the CPU suite.

    Six call sites each rebuilt this env dict by hand and each remembered
    GPU_LOCK and GPU_QLOG; a seventh variable had to be added to all six and
    was added to one. One definition, so the next knob cannot half-land.
    """
    env = dict(os.environ,
               GPU_LOCK=os.path.join(tmpdir, "gpu.lock"),
               GPU_QLOG=os.path.join(tmpdir, "queue.log"),
               GPU_PAUSE_FLAG=os.path.join(tmpdir, "paused"),
               # Fourth of its kind. Without this the suite wrote pending
               # markers into the machine's real queue directory, and
               # gpu_pause.sh dutifully reported four dead chains that were
               # only ever tests.
               GPU_PENDING_DIR=os.path.join(tmpdir, "pending"),
               # The card is REAL STATE too, and this is the third variable to
               # be found leaking in. With llama-server resident for an
               # unseen_books run (14.7 GB), every probe here was refused with
               # rc=7 NO_VRAM and eight tests failed - the suite reporting on
               # what the GPU happened to be doing rather than on gpu_job.sh.
               # Never in CI, which has no GPU and always reads VRAM_UNKNOWN.
               #
               # VramGateTest overrides this and supplies its own card through
               # _fake_gpu, so the gate itself stays tested; everyone else
               # stops caring what is running.
               REQUIRE_VRAM_GB="0")
    env.update(extra)
    return env


# ADVISORY MARKERS ARE NOT THE LIFECYCLE. gpu_job.sh writes notes alongside the
# QUEUED -> IDENT -> START -> OK/FAILED sequence: DIRTY_RUN when the tree gate
# is waived, VRAM_UNKNOWN when rocm-smi cannot answer, HELD/RELEASED when the
# queue is paused, LLM_UNCHECKED when the preflight cannot run.
#
# Which of those appear depends on the MACHINE, not on the code under test.
# VRAM_UNKNOWN never appears locally (this box has rocm-smi) and always appears
# in CI (no GPU), so two lifecycle tests passed here and failed there. They were
# already filtering DIRTY_RUN one marker at a time, which is how the next
# marker breaks them again.
#
# Filter the whole class once, in one place. A test about ORDER should assert
# the order, not the presence of environment-dependent notes.
ADVISORY_MARKERS = {"DIRTY_RUN", "VRAM_UNKNOWN", "LLM_UNCHECKED",
                    "HELD", "RELEASED", "PAUSED", "RESUMED", "STOPPED"}


def lifecycle(log_text):
    """-> the QUEUED/IDENT/START/OK markers, advisory notes removed."""
    kinds = [line.split()[1] for line in log_text.splitlines() if line.strip()]
    return [k for k in kinds if k not in ADVISORY_MARKERS]


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
        env = isolated_env(self.tmp.name, GPU_LOCK=lock or self.lock,
                           GPU_QLOG=self.qlog, ALLOW_DIRTY_TREE=allow_dirty)
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
        self.assertEqual(lifecycle(self.log()),
                         ["QUEUED", "IDENT", "START", "OK"])

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
        env = isolated_env(self.tmp.name, GPU_LOCK=self.lock,
                           GPU_QLOG=self.qlog, ALLOW_DIRTY_TREE="1")
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
        env = isolated_env(self.tmp.name, GPU_LOCK=self.lock,
                           GPU_QLOG=self.qlog, ALLOW_DIRTY_TREE="1")
        slow = subprocess.Popen(["bash", GPU_JOB, "slow", "sleep", "2"],
                                env=env, stdout=subprocess.DEVNULL)
        time.sleep(0.4)
        r = self.run_job("waiter", "true", timeout=60)
        slow.wait(timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("OK       waiter", self.log())

    def test_interrupting_a_waiter_releases_nothing_and_logs_no_start(self):
        """A job killed while queued must not appear to have run."""
        env = isolated_env(self.tmp.name, GPU_LOCK=self.lock,
                           GPU_QLOG=self.qlog, ALLOW_DIRTY_TREE="1")
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
        self.assertEqual(lifecycle(self.log()),
                         ["QUEUED", "IDENT", "START", "OK"])

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
                           env=isolated_env(self.tmp.name, GPU_LOCK=self.lock,
                                            GPU_QLOG=self.qlog,
                                            ALLOW_DIRTY_TREE="1"))
        self.assertEqual(r.returncode, 2)



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

    def _commit_artifact(self, name="probe.json"):
        """A committed artifact, so modifying it later is tracked-file dirt."""
        d = os.path.join(self.root, "ab_test_runtime", "experiments")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as handle:
            handle.write('{"rows": []}\n')
        self._git("add", "--", path)
        self._git("commit", "-qm", "artifact")
        return path

    def test_rewriting_an_artifact_is_not_dirt(self):
        """replay_dirty_evidence rewrites artifacts; that is its JOB.

        Before this, its second replay onward ran with a dirty tree, stamped
        dirty=True on evidence produced in order to BE clean, and every job
        queued behind it was REFUSED - 80 idle minutes on 2026-08-18. An
        output a run wrote is not evidence that its code changed.
        """
        self._make_repo(dirty=False)
        path = self._commit_artifact()
        with open(path, "w") as handle:
            handle.write('{"rows": [1, 2, 3]}\n')
        result = self._run()
        self.assertEqual(0, result.returncode,
                         f"a rewritten artifact refused the job: {result.stderr}")
        self.assertIn("tree=clean", self._log())

    def test_modified_code_is_still_dirt(self):
        """The half that must NOT be lost: excluding outputs is not amnesty."""
        self._make_repo(dirty=True)
        self._commit_artifact()
        result = self._run()
        self.assertEqual(5, result.returncode)
        self.assertIn("REFUSED", self._log())

    def test_an_overridden_dirty_run_saves_the_diff_that_produced_it(self):
        """The override is where code state is LEAST recoverable.

        ALLOW_DIRTY_TREE=1 used to log DIRTY_RUN and nothing else, so the next
        edit erased the only copy of the code that made the artifact - a note
        saying "this was dirty, good luck". W&B writes diff.patch relative to
        HEAD for exactly this reason; the patch plus the recorded commit
        rebuilds the tree.
        """
        self._make_repo(dirty=True)
        result = self._run(allow_dirty="1")
        self.assertEqual(0, result.returncode, result.stderr)
        patch_dir = os.path.join(self.root, "ab_test_runtime", "logs",
                                 "dirty_patches")
        patches = os.listdir(patch_dir)
        self.assertEqual(1, len(patches), f"expected one patch, got {patches}")
        with open(os.path.join(patch_dir, patches[0]), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("an uncommitted change", body,
                      "the patch does not contain the uncommitted change")
        self.assertIn(f"patch={patches[0]}", self._log(),
                      "the queue log must name the patch it saved")

    def test_the_saved_patch_includes_an_untracked_harness_script(self):
        """`git diff HEAD` cannot see a new file, and a new harness script
        being untracked while it runs is the case this gate exists for."""
        self._make_repo(dirty=True)
        self._add_untracked("brand_new_probe.py")
        self._run(allow_dirty="1")
        patch_dir = os.path.join(self.root, "ab_test_runtime", "logs",
                                 "dirty_patches")
        body = open(os.path.join(patch_dir, os.listdir(patch_dir)[0]),
                    encoding="utf-8").read()
        self.assertIn("brand_new_probe.py", body)
        self.assertIn("brand new, not yet committed", body)

    def test_a_clean_run_writes_no_patch(self):
        # Nothing to record, and a stray empty patch would suggest otherwise.
        self._make_repo(dirty=False)
        self._run()
        self.assertFalse(os.path.isdir(os.path.join(
            self.root, "ab_test_runtime", "logs", "dirty_patches")))

    def test_an_untracked_experiment_script_is_still_dirt(self):
        # The dangerous case the exclusion must not reach: a harness that
        # exists on one machine while producing a cited number.
        self._make_repo(dirty=False)
        self._commit_artifact()
        self._add_untracked("brand_new_probe.py")
        self.assertEqual(5, self._run().returncode)

    def _add_untracked(self, name):
        path = os.path.join(self.root, "app", "experiments", name)
        with open(path, "w") as handle:
            handle.write("# brand new, not yet committed\n")
        return path

    def _run(self, allow_dirty=None):
        env = isolated_env(self.root)
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
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           ALLOW_DIRTY_TREE="1", **extra)
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


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class VramGateTest(unittest.TestCase):
    """The lock serialises jobs; it says nothing about memory.

    On 2026-08-17 llama-server held 14.77 GiB of a 15.92 GiB card while
    regate_with_provenance held the lock, and 14 consecutive adapters died on

        HIP out of memory. Tried to allocate 2.00 MiB.
        GPU 0 has a total capacity of 15.92 GiB of which 0 bytes is free.

    ensure_llama_server.sh starts that server OUTSIDE the lock on purpose -
    "the CALLER never kills the server" is what lets consecutive LLM evals
    share one load - and it has no lifecycle end. So the lock's guarantee was
    true and useless: one job held the lock, a non-job held the memory.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qlog = os.path.join(self.tmp.name, "queue.log")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **extra):
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           ALLOW_DIRTY_TREE="1", **extra)
        return subprocess.run(["bash", GPU_JOB, "probe", "true"],
                              env=env, capture_output=True, text=True, timeout=60)

    def _log(self):
        return open(self.qlog, encoding="utf-8").read() if os.path.exists(self.qlog) else ""

    def _fake_gpu(self, total_bytes, used_bytes):
        """PATH entry whose rocm-smi reports a card of our choosing.

        These tests used to read the HOST's GPU and skipTest when there was
        none - so they measured a different thing on every machine and did not
        run at all in CI. verify_release rejects skips outright, correctly: a
        silently skipped test is one that is not running. Supplying the reading
        makes the gate testable anywhere and deterministic everywhere.
        """
        shadow = os.path.join(self.tmp.name, "fakegpu")
        os.makedirs(shadow, exist_ok=True)
        script = (
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *showpids*) echo '1234\tllama-server\t1\t%d'; exit 0;;\n"
            "esac\n"
            "echo 'GPU[0]\t\t: VRAM Total Memory (B): %d'\n"
            "echo 'GPU[0]\t\t: VRAM Total Used Memory (B): %d'\n"
        ) % (used_bytes, total_bytes, used_bytes)
        path = os.path.join(shadow, "rocm-smi")
        with open(path, "w") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        return shadow + os.pathsep + os.environ["PATH"]

    def test_a_job_is_refused_when_the_card_is_nearly_full(self):
        # 16 GiB card with 15 GiB gone: 1 GiB free against a 4 GiB need.
        gib = 1024 ** 3
        result = self._run(REQUIRE_VRAM_GB="4",
                           PATH=self._fake_gpu(16 * gib, 15 * gib))
        self.assertEqual(7, result.returncode)
        self.assertIn("NO_VRAM", self._log())
        self.assertNotIn("START", self._log(),
                         "the job must not start when the card is full")

    def test_the_same_job_runs_when_the_card_is_free(self):
        # The other half of the pair: identical job, roomy card.
        gib = 1024 ** 3
        result = self._run(REQUIRE_VRAM_GB="4",
                           PATH=self._fake_gpu(16 * gib, 2 * gib))
        self.assertEqual(0, result.returncode)
        self.assertIn("START", self._log())
        self.assertNotIn("NO_VRAM", self._log())

    def test_the_refusal_names_what_to_do_about_it(self):
        """A gate that only says no gets overridden reflexively."""
        gib = 1024 ** 3
        result = self._run(REQUIRE_VRAM_GB="4",
                           PATH=self._fake_gpu(16 * gib, 15 * gib))
        self.assertIn("llama-server", result.stderr,
                      "the usual cause must be named")
        self.assertIn("REQUIRE_VRAM_GB=0", result.stderr,
                      "the override must be discoverable")

    def test_zero_disables_the_check_for_small_jobs(self):
        self.assertEqual(0, self._run(REQUIRE_VRAM_GB="0").returncode)
        self.assertIn("START", self._log())

    def test_an_unreadable_gpu_warns_rather_than_blocking(self):
        # "Cannot tell" is not "no memory" - the same third answer tree_state
        # gives for a non-repo. A missing tool must never block the card.
        #
        # Shadowing rocm-smi with a failing stub, NOT emptying PATH: an empty
        # PATH also hides bash and the test then measures its own broken
        # fixture rather than the gate.
        shadow = os.path.join(self.tmp.name, "bin")
        os.makedirs(shadow, exist_ok=True)
        stub = os.path.join(shadow, "rocm-smi")
        with open(stub, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)
        result = self._run(PATH=shadow + os.pathsep + os.environ["PATH"],
                           REQUIRE_VRAM_GB="4096")
        self.assertEqual(0, result.returncode,
                         "an unreadable GPU must not stop the job")
        self.assertIn("VRAM_UNKNOWN", self._log())
        self.assertIn("START", self._log())


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class PauseAndProcessGroupTest(unittest.TestCase):
    """Holding the queue for other use, and taking children down with a job."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qlog = os.path.join(self.tmp.name, "queue.log")
        self.flag = os.path.join(self.tmp.name, "paused")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra):
        return isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                            GPU_PAUSE_FLAG=self.flag, ALLOW_DIRTY_TREE="1",
                            REQUIRE_VRAM_GB="0", **extra)

    def _log(self):
        return open(self.qlog, encoding="utf-8").read() if os.path.exists(self.qlog) else ""

    def test_a_paused_queue_holds_the_job_before_the_lock(self):
        """Waiting must happen BEFORE flock, or a held queue blocks everything
        while looking busy."""
        open(self.flag, "w").write("paused")
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(["bash", GPU_JOB, "probe", "true"],
                           env=self._env(), capture_output=True, timeout=8)
        self.assertIn("HELD", self._log())
        self.assertNotIn("START", self._log())

    def test_releasing_the_flag_lets_the_job_run(self):
        r = subprocess.run(["bash", GPU_JOB, "probe", "true"],
                           env=self._env(), capture_output=True, timeout=60)
        self.assertEqual(0, r.returncode)
        self.assertIn("START", self._log())

    def test_exit_codes_survive_the_process_group_wrapper(self):
        # Running the job via setsid must not swallow its status - the whole
        # contract of this script is that a failure propagates.
        r = subprocess.run(["bash", GPU_JOB, "probe", "bash", "-c", "exit 37"],
                           env=self._env(), capture_output=True, timeout=60)
        self.assertEqual(37, r.returncode)
        self.assertIn("FAILED   probe rc=37", self._log())

    def test_a_killed_job_takes_its_descendants_with_it(self):
        """Borrowed from codex's KillMode=control-group, and not theoretical:
        after the regate chain was killed on 2026-08-17 a python child kept
        2.11 GiB of the card and starved the next job."""
        marker = os.path.join(self.tmp.name, "alive")
        script = (f"bash -c 'while true; do touch {marker}; sleep 1; done' & "
                  "sleep 30")
        proc = subprocess.Popen(["bash", GPU_JOB, "probe", "bash", "-c", script],
                                env=self._env(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(4)
        proc.terminate()
        proc.wait(timeout=20)
        time.sleep(3)
        if os.path.exists(marker):
            os.unlink(marker)
        time.sleep(3)
        self.assertFalse(os.path.exists(marker),
                         "a descendant outlived the job and would hold the GPU")


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class HarnessIsolationTest(unittest.TestCase):
    """No test may consult the machine's real pause flag, lock or queue log.

    This is the bug that actually happened, not a hypothetical: the queue was
    paused to free the GPU for a game, and the whole suite - plus the release
    verifier - began erroring, because the harness inherited GPU_PAUSE_FLAG
    from the environment and every probe waited on it.

    Asserting on the env dict rather than on a run is deliberate. Proving it
    behaviourally means creating the flag at its DEFAULT path inside the repo,
    and a test that pauses the real queue is a test that can leave the real
    queue paused when it fails.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.captured = []

    def tearDown(self):
        self.tmp.cleanup()

    def _capture(self, build):
        """Run `build` with subprocess.run/Popen stubbed; return the envs used."""
        real_run, real_popen = subprocess.run, subprocess.Popen

        def fake(args, **kw):
            self.captured.append(kw.get("env") or {})
            return subprocess.CompletedProcess(args, 0, "", "")

        subprocess.run, subprocess.Popen = fake, fake
        try:
            build()
        finally:
            subprocess.run, subprocess.Popen = real_run, real_popen
        return self.captured

    def test_every_entry_point_isolates_all_three_shared_paths(self):
        harnesses = ((GpuJobTest, lambda t: t.run_job("probe", "true")),
                     (LlmPreflightGateTest, lambda t: t._run()),
                     (VramGateTest, lambda t: t._run()))

        for cls, call in harnesses:
            with self.subTest(harness=cls.__name__):
                inst = cls.__new__(cls)
                inst.tmp = tempfile.TemporaryDirectory()
                inst.lock = os.path.join(inst.tmp.name, "gpu.lock")
                inst.qlog = os.path.join(inst.tmp.name, "queue.log")
                self.captured = []
                envs = self._capture(lambda: call(inst))
                self.assertTrue(envs, f"{cls.__name__} spawned nothing")
                for env in envs:
                    for var in ("GPU_LOCK", "GPU_QLOG", "GPU_PAUSE_FLAG"):
                        self.assertTrue(
                            var in env and env[var].startswith(inst.tmp.name),
                            f"{cls.__name__} does not isolate {var}: "
                            f"{env.get(var, '<inherited>')}")
                inst.tmp.cleanup()

    def test_a_paused_machine_does_not_change_what_the_harness_sees(self):
        """The flag the tests use must never be the one gpu_pause.sh writes."""
        real = os.path.join(REPO, "ab_test_runtime", "logs", "gpu_paused")
        env = isolated_env(self.tmp.name)
        self.assertNotEqual(real, env["GPU_PAUSE_FLAG"])
        os.environ["GPU_PAUSE_FLAG"] = real
        try:
            self.assertNotEqual(real, isolated_env(self.tmp.name)["GPU_PAUSE_FLAG"],
                                "an inherited pause flag survived isolation")
        finally:
            os.environ.pop("GPU_PAUSE_FLAG", None)


GPU_PAUSE = os.path.join(REPO, "gpu_pause.sh")


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class TerminalMarkerTest(unittest.TestCase):
    """A job that STARTED must always end with a terminal line.

    THE MYSTERY THIS EXPLAINS. On 2026-08-17 e_row_e finished - artifact
    complete, 400 of 400, next arm queued a second later - and neither OK nor
    FAILED was ever written. The markers were echoed only on the normal
    fall-through, so any exit through the INT/TERM trap left a START with no
    terminal line, which nothing downstream can distinguish from a job still
    running. gpu_pause.sh insisted the card was busy for 24 minutes while it
    was idle, and that answer was relayed to the user as fact.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qlog = os.path.join(self.tmp.name, "queue.log")

    def _env(self, **extra):
        return isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                            ALLOW_DIRTY_TREE="1", **extra)

    def _log(self):
        if not os.path.exists(self.qlog):
            return ""
        with open(self.qlog, encoding="utf-8") as fh:
            return fh.read()

    def _terminal_lines(self):
        terminal = ("OK", "FAILED", "INTERRUPTED", "REFUSED", "NO_VRAM",
                    "NO_LLM", "LOCK_FAILED")
        return [l for l in self._log().splitlines()
                if len(l.split()) > 1 and l.split()[1] in terminal]

    def test_an_interrupted_job_still_records_a_terminal_line(self):
        """THE DEFECT, reproduced: signal the wrapper mid-run."""
        proc = subprocess.Popen(["bash", GPU_JOB, "victim", "sleep", "30"],
                                env=self._env(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        # Wait for START rather than for a fixed delay. The gates ahead of it
        # shell out to git and rocm-smi, which take seconds on a busy machine -
        # the first version of this test signalled during that window, saw a
        # log holding only QUEUED, and called the fix broken. The invariant is
        # "a job that STARTED ends with a terminal line", so the test must
        # actually get it started.
        deadline = time.time() + 90
        while time.time() < deadline and "START    victim" not in self._log():
            time.sleep(0.2)
        if "START    victim" not in self._log():
            proc.kill()
            self.skipTest("job never started; nothing to assert about ending")
        proc.terminate()
        proc.wait(timeout=60)
        self.assertEqual(1, len(self._terminal_lines()),
                         f"expected one terminal line, got: {self._log()}")
        self.assertIn("INTERRUPTED victim", self._log())

    def test_a_normal_run_records_exactly_one_terminal_line(self):
        # The EXIT trap fires after the normal path has already logged, so the
        # guard against double-logging is as load-bearing as the trap itself.
        subprocess.run(["bash", GPU_JOB, "fine", "true"], env=self._env(),
                       capture_output=True, timeout=60)
        self.assertEqual(["OK"], [l.split()[1] for l in self._terminal_lines()])

    def test_a_failing_run_records_exactly_one_terminal_line(self):
        subprocess.run(["bash", GPU_JOB, "bad", "bash", "-c", "exit 9"],
                       env=self._env(), capture_output=True, timeout=60)
        lines = self._terminal_lines()
        self.assertEqual(1, len(lines), lines)
        self.assertIn("FAILED   bad rc=9", lines[0])


@unittest.skipUnless(os.path.exists(GPU_JOB), "gpu_job.sh not present")
class PendingMarkerTest(unittest.TestCase):
    """A job WAITING and a chain that DIED must not look the same.

    Everything queued behind the lock is a blocked process that nothing lists,
    so twice on 2026-08-18 a dead chain left "QUEUED x" as the last line in the
    log while the card sat idle - 80 minutes, then another hour. task-spooler
    answers this with `ts -l`; these markers are the small version, one file
    per waiting job, removed however the job ends.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qlog = os.path.join(self.tmp.name, "queue.log")
        self.pending = os.path.join(self.tmp.name, "pending")

    def _run(self, *argv, **extra):
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           ALLOW_DIRTY_TREE="1", **extra)
        return subprocess.run(["bash", GPU_JOB, *argv], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_finished_job_leaves_no_marker_behind(self):
        before = set(os.listdir(self.pending)) if os.path.isdir(self.pending) else set()
        self._run("probe", "true")
        after = set(os.listdir(self.pending)) if os.path.isdir(self.pending) else set()
        self.assertEqual(before, after,
                         "a completed job left a pending marker claiming it waits")

    def test_a_failed_job_leaves_no_marker_behind(self):
        # The trap is on EXIT, not on success: a job that dies mid-queue is
        # exactly the case that produced a stale-looking queue.
        before = set(os.listdir(self.pending)) if os.path.isdir(self.pending) else set()
        self._run("probe", "bash", "-c", "exit 3")
        after = set(os.listdir(self.pending)) if os.path.isdir(self.pending) else set()
        self.assertEqual(before, after)

    def test_a_waiting_job_is_visible_while_it_waits(self):
        """The whole point: something must be able to say what is queued."""
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           ALLOW_DIRTY_TREE="1")
        # The holder must outlive the waiter's startup, and startup is not
        # instant: the gates ahead of the marker shell out to git and rocm-smi,
        # which took longer than the old 6-second hold when this machine was
        # running three chains and a generation at once. The test then reported
        # "the waiting job was not listed" - a real-looking failure of the
        # feature, caused by the fixture. Same shape as the terminal-marker
        # test's first version, and the fix is the same: wait for the state
        # you need instead of assuming a duration.
        holder = subprocess.Popen(["bash", GPU_JOB, "holder", "sleep", "45"],
                                  env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        waiter = subprocess.Popen(["bash", GPU_JOB, "waiter", "true"],
                                  env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 60
            names = []
            while time.time() < deadline:
                if os.path.isdir(self.pending):
                    names = [n for n in os.listdir(self.pending)
                             if n.endswith(("waiter", "holder"))]
                    if any(n.endswith("waiter") for n in names):
                        break
                time.sleep(0.2)
            self.assertTrue(any(n.endswith("waiter") for n in names),
                            f"the waiting job was not listed: {names}")
        finally:
            holder.terminate()          # release the lock so the waiter ends
            waiter.wait(timeout=60)
            holder.wait(timeout=60)

    def _fake_pterm(self, record):
        """A pterm on PATH that appends a line per notification."""
        shadow = os.path.join(self.tmp.name, "notifier")
        os.makedirs(shadow, exist_ok=True)
        path = os.path.join(shadow, "pterm")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/bash\necho "$@" >> {record}\n')
        os.chmod(path, 0o755)
        return shadow

    def test_repeated_jobs_do_not_produce_one_alert_each(self):
        """THE COMPLAINT. This fires per job, and a chain is many jobs.

        The re-gate runs 67 of them, each finishing with nothing queued behind
        it in that instant, so it produced 67 desktop alerts saying the same
        thing. One per quiet period is the information; the rest train you to
        ignore all of them.
        """
        record = os.path.join(self.tmp.name, "alerts")
        shadow = self._fake_pterm(record)
        for _ in range(3):
            self._run("probe", "true",
                      PATH=shadow + os.pathsep + os.environ["PATH"])
        alerts = open(record).read().splitlines() if os.path.exists(record) else []
        self.assertEqual(1, len(alerts), f"expected one alert, got {alerts}")

    def test_the_cooldown_can_be_shortened_for_a_genuinely_new_event(self):
        record = os.path.join(self.tmp.name, "alerts")
        shadow = self._fake_pterm(record)
        for _ in range(2):
            self._run("probe", "true", GPU_NOTIFY_COOLDOWN="0",
                      PATH=shadow + os.pathsep + os.environ["PATH"])
        alerts = open(record).read().splitlines() if os.path.exists(record) else []
        self.assertEqual(2, len(alerts))

    def test_notifications_can_be_turned_off_entirely(self):
        # A courtesy must never be something the user has to tolerate.
        record = os.path.join(self.tmp.name, "alerts")
        shadow = self._fake_pterm(record)
        self._run("probe", "true", GPU_NOTIFY="0",
                  PATH=shadow + os.pathsep + os.environ["PATH"])
        self.assertFalse(os.path.exists(record))

    def test_a_job_runs_at_low_priority_so_the_desktop_wins(self):
        """Background work must lose to whatever the user is doing.

        Nothing set priority, so a job competed with the foreground as an
        equal: a book generation held llama-server at 169% CPU while a game
        ran, load average near 9, and pausing the QUEUE could not help because
        the already-running job was scheduled at parity.
        """
        result = self._run("niced", "bash", "-c", "ps -o ni= -p $$")
        niceness = [int(line) for line in result.stdout.split()
                    if line.lstrip("-").isdigit()]
        self.assertTrue(niceness, f"no niceness reported: {result.stdout!r}")
        # Relative for the same reason - but niceness CAPS AT 19, so a suite
        # already running there cannot observe an increase at all. Assert what
        # is observable from where the test happens to be standing rather than
        # a fixed number that is right in one context and wrong in the other.
        mine = os.nice(0)
        if mine >= 19:
            self.assertEqual(19, niceness[0])
        else:
            self.assertGreater(niceness[0], mine)

    def test_the_priority_can_be_waived_for_a_run_that_should_compete(self):
        """Waived means "add nothing", not "reset to zero".

        Niceness is inherited, and this suite is often run niced itself - the
        first version asserted 0 and failed at 19, which was the harness's own
        priority rather than a broken waiver. Compare against the parent.
        """
        mine = os.nice(0)
        result = self._run("greedy", "bash", "-c", "ps -o ni= -p $$",
                           GPU_JOB_NICE="0")
        niceness = [int(line) for line in result.stdout.split()
                    if line.lstrip("-").isdigit()]
        self.assertTrue(niceness, result.stdout)
        self.assertEqual(mine, niceness[0])

    def test_a_notifier_that_is_absent_or_broken_cannot_change_the_exit_code(self):
        """Best-effort means best-effort: reporting must never fail the job.

        A notification is a courtesy; a job whose result was destroyed by its
        own progress report is a worse outcome than silence.
        """
        shadow = os.path.join(self.tmp.name, "badbin")
        os.makedirs(shadow, exist_ok=True)
        fake = os.path.join(shadow, "pterm")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\nexit 9\n")
        os.chmod(fake, 0o755)
        r = self._run("probe", "true", PATH=shadow + os.pathsep + os.environ["PATH"])
        self.assertEqual(0, r.returncode, r.stderr)


@unittest.skipUnless(os.path.exists(GPU_PAUSE), "gpu_pause.sh not present")
class PauseStatusTest(unittest.TestCase):
    """`status` answers "is the card busy?" - so it must not guess.

    Nothing tested gpu_pause.sh at all before this, which is how a feature
    reviewed at /code-review20 shipped with two defects that only appear once
    somebody actually pauses.

    The defect: running_job read the queue log alone. A job whose START never
    got a matching OK - which happened for real to e_row_e on 2026-08-17,
    artifact complete, cause of the missing marker still unknown - is reported
    as running forever. Someone waiting for the GPU to free up waits on a job
    that ended half an hour ago.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qlog = os.path.join(self.tmp.name, "queue.log")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_log(self, *lines):
        with open(self.qlog, "w", encoding="utf-8") as fh:
            fh.write("".join(f"2026-08-17T00:00:0{i}Z {l}\n"
                             for i, l in enumerate(lines)))

    def _status(self):
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           GPU_PAUSE_FLAG=os.path.join(self.tmp.name, "paused"))
        return subprocess.run(["bash", GPU_PAUSE, "status"], env=env,
                              capture_output=True, text=True, timeout=30).stdout

    def test_a_start_with_no_result_and_no_process_is_not_called_running(self):
        """THE DEFECT, verbatim: e_row_e's log state, replayed."""
        self._write_log("QUEUED   e_row_e", "START    e_row_e",
                        "QUEUED   e_row_ay", "HELD     e_row_ay (queue paused)")
        out = self._status()
        self.assertIn("running job: none", out)
        self.assertIn("e_row_e", out, "the disagreement must still be reported")
        self.assertIn("finished without", out)

    def test_a_completed_job_is_not_running(self):
        self._write_log("START    done_job", "OK       done_job")
        self.assertIn("running job: none", self._status())

    def test_a_job_with_a_live_process_is_reported_as_running(self):
        """The other half: a real job must not be dismissed as stale."""
        env = isolated_env(self.tmp.name, GPU_QLOG=self.qlog,
                           ALLOW_DIRTY_TREE="1", REQUIRE_VRAM_GB="0")
        proc = subprocess.Popen(["bash", GPU_JOB, "sleeper", "sleep", "20"],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                if os.path.exists(self.qlog):
                    with open(self.qlog, encoding="utf-8") as fh:
                        if "START    sleeper" in fh.read():
                            break
                time.sleep(0.2)
            self.assertIn("running job: sleeper", self._status())
        finally:
            proc.terminate()
            proc.wait(timeout=20)

    def test_status_never_reports_itself_as_the_running_job(self):
        # pgrep -f matches whole command lines, including the one doing the
        # matching. Self-matching killed this session's shell twice.
        self._write_log("START    status", "QUEUED   next")
        self.assertIn("running job: none", self._status())


if __name__ == "__main__":
    # AT THE END, because it was in the MIDDLE. Everything defined after it -
    # four of the five test classes here - was invisible to `python
    # tests/test_gpu_job.py`, which ran the first class and printed OK. Under
    # `unittest discover` the module is imported, __name__ is not "__main__",
    # and all five classes register, so this hid only from whoever ran the
    # file directly to check their work. Same family as the tests/ package
    # needing __init__.py: a runner that silently tests less than you think.
    unittest.main()
