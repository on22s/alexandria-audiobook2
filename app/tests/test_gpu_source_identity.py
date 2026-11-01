"""Native repositories discriminate input identities from generated outputs."""
from pathlib import Path
import re
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from experiments import manifest
from experiments.manifest import _git_state
from tests import test_gpu_job as gpu_job_tests
from tests.test_support import requires_linux_queue_tools


class SourceIdentityTests(unittest.TestCase):
    setUp = gpu_job_tests.DirtyTreeGateTest.setUp
    tearDown = gpu_job_tests.DirtyTreeGateTest.tearDown
    _git = gpu_job_tests.DirtyTreeGateTest._git
    _run = gpu_job_tests.DirtyTreeGateTest._run
    _log = gpu_job_tests.DirtyTreeGateTest._log

    def prepare(self):
        gpu_job_tests.DirtyTreeGateTest._make_repo(self, dirty=False)

    def state(self):
        result = self._run(allow_dirty="1")
        self.assertEqual(0, result.returncode, result.stderr)
        identity = [line for line in self._log().splitlines() if " IDENT " in line][-1]
        return re.search(r"tree=([^ ]+)", identity).group(1)

    @requires_linux_queue_tools
    def test_every_nonignored_input_kind_is_refused_and_recorded(self):
        self.prepare()
        for name in ("kernel.cu", "config.toml", "Makefile", "fixture.json", "notebook.ipynb", "notes.md", "text input.txt", "executable"):
            with self.subTest(name=name):
                path = Path(self.root, name)
                path.write_text("uncommitted input\n")
                path.chmod(0o755 if name == "executable" else 0o644)
                try:
                    result = self._run()
                    self.assertEqual(5, result.returncode, result.stderr)
                    state = _git_state(self.root)
                    self.assertTrue(state["dirty"])
                    self.assertIn(name, state["untracked_harness_files"])
                finally:
                    path.unlink()

    @requires_linux_queue_tools
    def test_same_count_inputs_hash_their_contents_and_names(self):
        self.prepare()
        path = Path(self.root, "new.py")
        path.write_text("VALUE = 1\n")
        first = self.state()
        path.write_text("VALUE = 2\n")
        second = self.state()
        path.rename(Path(self.root, "other.py"))
        third = self.state()
        self.assertEqual(3, len({first, second, third}))
        self.assertEqual(third, self.state(), "unchanged inputs must keep the same identity")

    @requires_linux_queue_tools
    def test_modified_tracked_contents_change_identity(self):
        self.prepare()
        path = Path(self.root, "app/experiments/kept.py")
        path.write_text("TRACKED = 1\n")
        first = self.state()
        path.write_text("TRACKED = 2\n")
        self.assertNotEqual(first, self.state())

    @requires_linux_queue_tools
    def test_binary_untracked_and_odd_names_are_restorable_from_patch(self):
        self.prepare()
        names = ("binary fixture.json", "line\nbreak.toml", "-leading.cu")
        payload = b"\x00\x01native fixture\xff"
        for name in names:
            Path(self.root, name).write_bytes(payload)
        self.state()
        state = _git_state(self.root)
        self.assertEqual(set(names), set(state["untracked_harness_files"]))
        patches = list(Path(self.root, "dirty_patches").glob("*.patch"))
        self.assertTrue(patches)
        for name in names:
            Path(self.root, name).unlink()
        self._git("apply", str(patches[-1]))
        for name in names:
            self.assertEqual(payload, Path(self.root, name).read_bytes())

    @requires_linux_queue_tools
    def test_generated_runtime_files_remain_excluded(self):
        self.prepare()
        p = Path(self.root, "ab_test_runtime/views/probe.toml")
        p.parent.mkdir(parents=True)
        p.write_text("generated = true\n")
        self.assertEqual("clean", self.state())
        self.assertFalse(_git_state(self.root)["dirty"])

    def test_unavailable_source_probe_is_unknown_and_cannot_certify_clean(self):
        from tests.test_experiment_manifest import _record
        self.prepare()
        record = _record()
        with patch("experiments.manifest.subprocess.run", side_effect=OSError("source probe unavailable")):
            state = _git_state(self.root)
        self.assertIsNone(state["dirty"])
        self.assertEqual("unknown", state["source_state"])
        # Diagnosable, bounded, and never a dump of the environment.
        detail = state["source_state_detail"]
        self.assertIsInstance(detail, str)
        self.assertTrue(detail)
        self.assertLessEqual(len(detail), 200)
        self.assertNotIn("PATH=", detail)
        record.meta["git"] = state
        self.assertIn("tree source state could not be verified", record.validate({"require_clean_tree": True}))

    @requires_linux_queue_tools
    def test_failed_untracked_query_refuses_instead_of_certifying_clean(self):
        self.prepare()
        real_git = shutil.which("git")
        provider = Path(self.root, "git-provider")
        provider.mkdir()
        script = provider / "git"
        script.write_text("#!/bin/sh\nfor arg do\n  [ \"$arg\" = ls-files ] && exit 12\ndone\nexec " + shlex.quote(real_git) + " \"$@\"\n")
        script.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(provider) + os.pathsep + os.environ["PATH"]}):
            result = self._run()
            state = _git_state(self.root)
        self.assertEqual(5, result.returncode, result.stderr)
        self.assertIn("tree=dirty:unknown", self._log())
        self.assertNotIn("START", self._log())
        self.assertTrue(state["dirty"])
        self.assertEqual("dirty:unknown", state["source_state"])
        self.assertIn("dirty identity could not be computed",
                      state["source_state_detail"])


class SourceProbeShellTests(unittest.TestCase):
    """One locator chooses the interpreter; the classification stays in bash."""

    @staticmethod
    def completed(platform=b"MINGW64_NT-10.0\n", returncode=0):
        return mock.Mock(returncode=returncode, stdout=platform, stderr=b"")

    def test_explicit_git_bash_is_used_when_path_bash_is_absent(self):
        with patch("experiments.manifest._host_is_windows", return_value=True), \
                patch("experiments.manifest.shutil.which", return_value=None), \
                patch("experiments.manifest.subprocess.run",
                      return_value=self.completed()) as run, \
                patch.dict(os.environ,
                           {"AA2_SOURCE_PROBE_BASH": sys.executable}):
            shell, reason = manifest._source_probe_shell()
        self.assertIsNone(reason)
        self.assertEqual(sys.executable, shell)
        self.assertEqual([sys.executable, "-c", "uname -s"],
                         run.call_args[0][0])

    def test_explicit_linux_shell_is_refused_without_silent_fallback(self):
        with patch("experiments.manifest._host_is_windows", return_value=True), \
                patch("experiments.manifest.shutil.which",
                      return_value=sys.executable), \
                patch("experiments.manifest.subprocess.run",
                      return_value=self.completed(b"Linux\n")), \
                patch.dict(os.environ,
                           {"AA2_SOURCE_PROBE_BASH": sys.executable}):
            shell, reason = manifest._source_probe_shell()
        self.assertIsNone(shell, "a refused override must not fall back")
        self.assertIn("AA2_SOURCE_PROBE_BASH", reason)
        self.assertIn("not a Git-for-Windows/MSYS bash", reason)

    def test_existing_git_for_windows_candidate_wins_when_path_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp, "Git", "bin", "bash.exe")
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            environment = {"ProgramFiles": tmp, "ProgramW6432": "",
                           "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}
            with patch("experiments.manifest._host_is_windows",
                       return_value=True), \
                    patch("experiments.manifest.shutil.which",
                          return_value=None), \
                    patch("experiments.manifest.subprocess.run",
                          return_value=self.completed()), \
                    patch.dict(os.environ, environment):
                os.environ.pop("AA2_SOURCE_PROBE_BASH", None)
                shell, reason = manifest._source_probe_shell()
        self.assertIsNone(reason)
        self.assertEqual(str(candidate), shell)

    def test_missing_override_does_not_fall_back_to_another_shell(self):
        missing = os.path.join(tempfile.gettempdir(), "not-a-real-bash")
        for override in (missing, ""):
            with self.subTest(override=override), \
                    patch("experiments.manifest.shutil.which",
                          return_value=sys.executable) as fallback, \
                    patch.dict(os.environ, {"AA2_SOURCE_PROBE_BASH": override}):
                shell, reason = manifest._source_probe_shell()
                self.assertIsNone(shell)
                self.assertIn("not an executable file", reason)
                fallback.assert_not_called()
