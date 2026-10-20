"""Failing native patch commands must never advertise a saved reproduction."""
import os
from pathlib import Path
import shlex
import shutil
import unittest
from unittest.mock import patch

from tests import test_gpu_job as queue_tests


class DirtyPatchCaptureTests(unittest.TestCase):
    setUp = queue_tests.DirtyTreeGateTest.setUp
    tearDown = queue_tests.DirtyTreeGateTest.tearDown
    _git = queue_tests.DirtyTreeGateTest._git
    _run = queue_tests.DirtyTreeGateTest._run
    _log = queue_tests.DirtyTreeGateTest._log

    def inject(self, name, body):
        queue_tests.DirtyTreeGateTest._make_repo(self, dirty=True)
        Path(self.root, "new-input.txt").write_text("must be recoverable\n")
        self.providers = Path(self.root, "dirty_patches", "providers")
        self.providers.mkdir(parents=True)
        tool = self.providers / name
        tool.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        tool.chmod(0o755)

    def assert_unsaved(self):
        with patch.dict(os.environ, {"PATH": str(self.providers) + os.pathsep + os.environ["PATH"]}):
            result = self._run(allow_dirty="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("patch=UNSAVED", self._log())
        self.assertIn("START", self._log(), "the explicit dirty override still permits the job")
        self.assertNotIn("uncommitted state saved", result.stderr)
        self.assertNotIn("reproduce with:", result.stderr)
        self.assertEqual([], list(Path(self.root, "dirty_patches").glob("*.patch")))
        self.assertEqual([], list(Path(self.root, "dirty_patches").glob("*.patch.tmp.*")))

    def test_failed_tracked_diff_cannot_publish_empty_patch(self):
        real_git = shlex.quote(shutil.which("git"))
        self.inject("git", 'case "$*" in *" diff --binary HEAD") exit 128;; esac\n'
                    + 'exec ' + real_git + ' "$@"\n')
        self.assert_unsaved()

    def test_failed_untracked_diff_cannot_publish_tracked_only_patch(self):
        real_git = shlex.quote(shutil.which("git"))
        self.inject("git", 'case " $* " in *" diff --no-index "*) exit 2;; esac\n'
                    + 'exec ' + real_git + ' "$@"\n')
        self.assert_unsaved()

    def test_failed_capture_listing_cannot_silently_omit_untracked_inputs(self):
        real_git = shlex.quote(shutil.which("git"))
        self.inject("git", '''case " $* " in *" ls-files "*)
counter="$GPU_PATCH_DIR/list-count"
n=0
[ ! -f "$counter" ] || n=$(cat "$counter")
n=$((n + 1))
printf '%s' "$n" > "$counter"
[ "$n" -lt 2 ] || exit 12
;; esac
''' + 'exec ' + real_git + ' "$@"\n')
        self.assert_unsaved()

    def test_patch_output_redirection_failure_is_unsaved(self):
        real_mktemp = shlex.quote(shutil.which("mktemp"))
        # Replace the allocated output with a directory before redirection,
        # simulating a failed output open after successful directory admission.
        self.inject("mktemp", '''case "$*" in *".patch.tmp."*)
mkdir -p "$GPU_PATCH_DIR/blocked"
printf '%s\\n' "$GPU_PATCH_DIR/blocked"
exit 0
;; esac
''' + 'exec ' + real_mktemp + ' "$@"\n')
        self.assert_unsaved()

    def test_patch_rename_failure_does_not_advertise_partial_evidence(self):
        real_mv = shlex.quote(shutil.which("mv"))
        self.inject("mv", 'case "$*" in *".patch.tmp."*) exit 1;; esac\n'
                    + 'exec ' + real_mv + ' "$@"\n')
        self.assert_unsaved()


if __name__ == "__main__":
    unittest.main()
