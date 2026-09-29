"""The Muse server must enter the GPU queue before it starts."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent.parent
CHAIN = ROOT / "run_chains/muse_local_reasoninglow_20260914.sh"


class MuseGpuLockTests(unittest.TestCase):
    def test_server_start_queues_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = root / "run_chains/muse_local_reasoninglow_20260914.sh"
            chain.parent.mkdir()
            chain.write_text(CHAIN.read_text(encoding="utf-8"), encoding="utf-8")
            (root / "run_chains/lib").mkdir()
            (root / "run_chains/lib/stage.sh").write_text(
                "stage_note() { :; }\n"
                "run_stage() { printf '%s\\n' \"$*\" >> \"$STAGE_CALLS\"; }\n"
                "stage_commit_artifacts() { :; }\n"
                "stage_summary() { :; }\n", encoding="utf-8")
            model = root / "ab_test_runtime/models/muse-q3/Muse-Glimmer-30B-UD-Q3_K_XL.gguf"
            model.parent.mkdir(parents=True)
            model.write_text("model", encoding="utf-8")
            server = root / "llama-server"
            server.write_text("#!/bin/bash\ntouch \"$SERVER_STARTED\"\nexec sleep 30\n", encoding="utf-8")
            server.chmod(0o755)
            wrapper = root / "gpu_job.sh"
            wrapper.write_text("#!/bin/bash\nprintf '%s\\n' \"$*\" > \"$GPU_DISPATCH\"\n",
                               encoding="utf-8")
            wrapper.chmod(0o755)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("curl", "pkill", "sleep"):
                command = bin_dir / name
                command.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                command.chmod(0o755)
            env = {**os.environ, "LLAMA_BIN": str(server),
                   "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "GPU_DISPATCH": str(root / "dispatch.log"),
                   "SERVER_STARTED": str(root / "server_started"),
                   "STAGE_CALLS": str(root / "stages.log")}
            env.pop("ALEXANDRIA_GPU_LOCK_HELD", None)
            result = subprocess.run(["bash", str(chain)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("muse_local_reasoninglow_20260914", (root / "dispatch.log").read_text())
            self.assertFalse((root / "server_started").exists())
            env["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
            result = subprocess.run(["bash", str(chain)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "server_started").exists())
            stages = (root / "stages.log").read_text()
            self.assertEqual(4, len(stages.splitlines()))
            self.assertNotIn("gpu_job.sh", stages)


if __name__ == "__main__":
    unittest.main()
