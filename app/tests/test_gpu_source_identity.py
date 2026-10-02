"""Native repositories discriminate input identities from generated outputs."""
from pathlib import Path
import re
import os
import shlex
import shutil
import unittest
from unittest.mock import patch

from experiments.manifest import _git_state
from tests import test_gpu_job as gpu_job_tests


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

    def test_modified_tracked_contents_change_identity(self):
        self.prepare()
        path = Path(self.root, "app/experiments/kept.py")
        path.write_text("TRACKED = 1\n")
        first = self.state()
        path.write_text("TRACKED = 2\n")
        self.assertNotEqual(first, self.state())

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
        record.meta["git"] = state
        self.assertIn("tree source state could not be verified", record.validate({"require_clean_tree": True}))

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
