"""Linked worktrees contend on one native lock without depending on Git at runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from alexandria_file_lock import get_default_gpu_lock_path, acquire_exclusive_file_lock, release_exclusive_file_lock


class WorktreeGpuLockTests(unittest.TestCase):
    def test_linked_worktree_and_primary_share_actual_kernel_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary with spaces"
            linked = Path(tmp) / "linked"
            primary.mkdir()
            def git(*args):
                subprocess.run(["git", "-C", str(primary), *args], check=True, capture_output=True)
            git("init", "-q")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
            git("worktree", "add", "--detach", str(linked))
            path = Path(get_default_gpu_lock_path(primary))
            self.assertEqual(path, Path(get_default_gpu_lock_path(linked)))
            path.parent.mkdir(parents=True)
            with path.open("a+b") as held:
                acquire_exclusive_file_lock(held.fileno())
                env = {**os.environ, "PYTHONPATH": str(REPO), "PATH": ""}
                probe = "from alexandria_file_lock import *;import sys;f=open(get_default_gpu_lock_path(sys.argv[1]),'a+b');acquire_exclusive_file_lock(f.fileno())"
                blocked = subprocess.run([sys.executable, "-c", probe, str(linked)], env=env, capture_output=True)
                self.assertNotEqual(0, blocked.returncode, blocked.stderr)
                release_exclusive_file_lock(held.fileno())
                admitted = subprocess.run([sys.executable, "-c", probe, str(linked)], env=env, capture_output=True)
                self.assertEqual(0, admitted.returncode, admitted.stderr)

    def test_stage4_loads_without_preexisting_app_pythonpath(self):
        env = {**os.environ, "PYTHONPATH": ""}
        result = subprocess.run([sys.executable, "-c", "import runpy;d=runpy.run_path('run_stage4_checkpoint.py',run_name='startup_probe');assert callable(d['require_results_index_entries'])"],
                                cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
