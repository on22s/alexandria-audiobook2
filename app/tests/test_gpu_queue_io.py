"""Native queue admission and completion under real filesystem write failures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_gpu_job import GPU_JOB, isolated_env


class QueueIoFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "worker-ran"
        self.qlog = self.root / "queue.log"
        self.pending = self.root / "pending"
        provider = self.root / "bin"
        provider.mkdir()
        for name, source in {
            "rocm-smi": "#!/bin/sh\necho 'GPU[0]: VRAM Total Memory (B): 17179869184'\necho 'GPU[0]: VRAM Total Used Memory (B): 0'\n",
            "nvidia-smi": "#!/bin/sh\nexit 1\n",
        }.items():
            path = provider / name
            path.write_text(source)
            path.chmod(0o755)
        self.env = isolated_env(self.tmp.name, ALLOW_DIRTY_TREE="1",
                                GPU_NOTIFY="0", PATH=str(provider) + os.pathsep + os.environ["PATH"])

    def run_job(self, *, name="probe", worker=None, prefix=None):
        command = worker or [sys.executable, "-c",
                            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                            str(self.output)]
        argv = ["bash", GPU_JOB, name, *command]
        if prefix:
            argv = ["bash", "-c", prefix, "fixture", GPU_JOB, *command]
        return subprocess.run(argv, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def assert_refused(self, result):
        self.assertEqual(8, result.returncode, result.stderr)
        self.assertFalse(self.output.exists(), result.stderr)
        self.assertFalse(list(self.pending.glob("*.probe")))

    def test_unwritable_initial_log_never_launches_worker(self):
        for mode in ("directory", "missing-parent", "full"):
            with self.subTest(mode=mode):
                destination = self.root / mode
                if mode == "directory":
                    destination.mkdir()
                elif mode == "missing-parent":
                    destination = destination / "queue.log"
                else:
                    destination.symlink_to("/dev/full")
                self.env["GPU_QLOG"] = str(destination)
                self.assert_refused(self.run_job())

    def test_pending_directory_failure_never_launches_worker(self):
        self.pending.write_text("not a directory")
        result = self.run_job()
        self.assert_refused(result)
        self.assertIn("pending", result.stderr)

    def test_pending_filename_failure_never_launches_worker(self):
        result = self.run_job(name="n" * 256)
        self.assert_refused(result)
        self.assertIn("pending", result.stderr)

    def test_pending_write_failure_removes_partial_marker(self):
        self.pending.mkdir()
        result = self.run_job(prefix='ln -s /dev/full "$GPU_PENDING_DIR/$$.probe"; exec bash "$1" probe "${@:2}"')
        self.assert_refused(result)
        self.assertEqual([], list(self.pending.iterdir()))

    def test_start_record_failure_never_launches_worker(self):
        provider = self.root / "bin" / "rocm-smi"
        provider.write_text("#!/bin/sh\ncase \"$*\" in *showmeminfo*) rm -f \"$GPU_QLOG\"; mkdir \"$GPU_QLOG\";; esac\necho 'GPU[0]: VRAM Total Memory (B): 17179869184'\necho 'GPU[0]: VRAM Total Used Memory (B): 0'\n")
        self.assert_refused(self.run_job())

    def test_terminal_record_failure_is_not_reported_as_success(self):
        worker = [sys.executable, "-c",
                  "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran'); p=pathlib.Path(sys.argv[2]); p.unlink(); p.symlink_to('/dev/full')",
                  str(self.output), str(self.qlog)]
        result = self.run_job(worker=worker)
        self.assertEqual(8, result.returncode, result.stderr)
        self.assertTrue(self.output.exists())
        self.assertIn("queue log", result.stderr)
        self.assertIn("OK", result.stderr)
        self.assertEqual([], list(self.pending.iterdir()))
