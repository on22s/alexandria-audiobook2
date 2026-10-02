"""Real Bash PATH resolution and admission of safe queue names."""
import hashlib
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_gpu_job import GPU_JOB, isolated_env, copy_gpu_owner


class QueuePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo with spaces"
        self.cwd = self.root / "elsewhere"
        self.repo.mkdir()
        self.cwd.mkdir()
        self.script = self.repo / "gpu_job.sh"
        self.script.write_bytes(Path(GPU_JOB).read_bytes())
        self.script.chmod(0o755)
        copy_gpu_owner(str(self.repo))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in {"rocm-smi": "exit 1", "nvidia-smi": "exit 1"}.items():
            p = self.bin / name
            p.write_text("#!/bin/sh\n" + body + "\n")
            p.chmod(0o755)
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if key.startswith("GPU_") or key in ("ALLOW_DIRTY_TREE", "REQUIRE_LLM", "REQUIRE_VRAM_GB", "ALEXANDRIA_GPU_LOCK_HELD", "ALEXANDRIA_GPU_LOCK_PID"):
                self.env.pop(key)
        self.env.update(PATH=str(self.repo) + os.pathsep + str(self.bin) + os.pathsep + os.environ["PATH"], GPU_NOTIFY="0", REQUIRE_VRAM_GB="0")

    def run_wrapper(self, command, *args):
        return subprocess.run([*command, *args], cwd=self.cwd, env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_all_launch_forms_share_lock_log_pending_and_identity(self):
        alias = self.bin / "queue-alias"
        alias.symlink_to(self.script)
        commands = [["gpu_job.sh"], ["bash", "gpu_job.sh"],
                    ["bash", str(self.script)], ["bash", "../repo with spaces/gpu_job.sh"], [str(alias)]]
        logs = self.repo / "ab_test_runtime/logs"
        for command in commands:
            with self.subTest(command=command):
                for stray in (self.cwd / "ab_test_runtime", self.bin / "ab_test_runtime"):
                    shutil.rmtree(stray, ignore_errors=True)
                for flag, filename in (("--print-lock", "alexandria_gpu.lock"), ("--print-qlog", "gpu_jobq.log")):
                    result = self.run_wrapper(command, flag)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(str(logs / filename), result.stdout.strip())
                result = self.run_wrapper(command, "safe name", "true")
                self.assertEqual(0, result.returncode, result.stderr)
                text = (logs / "gpu_jobq.log").read_text()
                self.assertIn("gpu_job_sha=" + hashlib.sha256(self.script.read_bytes()).hexdigest()[:12], text)
                self.assertEqual([], list((logs / "pending").iterdir()))
                self.assertFalse((self.cwd / "ab_test_runtime").exists())
                self.assertFalse((self.bin / "ab_test_runtime").exists())

    def test_bash_path_launch_obeys_repository_pause_flag(self):
        logs = self.repo / "ab_test_runtime/logs"
        logs.mkdir(parents=True)
        (logs / "gpu_paused").write_text("paused")
        worker = self.root / "worker-ran"
        process = subprocess.Popen(["bash", "gpu_job.sh", "paused", "touch", str(worker)],
                                   cwd=self.cwd, env=self.env, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            qlog = logs / "gpu_jobq.log"
            while time.monotonic() < deadline:
                if qlog.exists() and "HELD" in qlog.read_text():
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertIsNone(process.poll(), "paused worker must wait")
            self.assertIn("HELD", qlog.read_text())
            self.assertFalse(worker.exists())
            self.assertTrue(list((logs / "pending").iterdir()))
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        self.assertEqual(143, process.returncode)
        self.assertEqual([], list((logs / "pending").iterdir()))

    def test_bash_path_launch_finds_repository_preflight(self):
        preflight = self.repo / "app/experiments/llm_preflight.py"
        preflight.parent.mkdir(parents=True)
        preflight.write_text("raise SystemExit(1)\n")
        interpreter = self.repo / "app/env/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        self.env["REQUIRE_LLM"] = "1"
        for override in (True, False):
            with self.subTest(interpreter_override=override):
                if override:
                    self.env["LLM_PREFLIGHT_PYTHON"] = sys.executable
                else:
                    self.env.pop("LLM_PREFLIGHT_PYTHON")
                result = self.run_wrapper(["bash", "gpu_job.sh"], "preflight", "true")
                self.assertEqual(6, result.returncode, result.stderr)
                self.assertIn("NO_LLM", (self.repo / "ab_test_runtime/logs/gpu_jobq.log").read_text())


class QueueNameTests(unittest.TestCase):
    def test_control_and_path_names_are_rejected_before_queue_records(self):
        for name in ("../escape", "nested/probe", "back\\slash", "x\ny", "x\ty", "x\ry", "x\x1by", "x\x7fy"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = isolated_env(tmp, ALLOW_DIRTY_TREE="1", GPU_NOTIFY="0")
                marker = root / "worker-ran"
                result = subprocess.run(["bash", GPU_JOB, name, "touch", str(marker)],
                                        env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("job name", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse((root / "queue.log").exists())
                self.assertFalse((root / "pending").exists())
