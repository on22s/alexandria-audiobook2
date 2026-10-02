"""The Muse server must enter the GPU queue before it starts."""
import os
import shutil
import sys
import socket
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent.parent
CHAIN = ROOT / "run_chains/muse_local_reasoninglow_20260914.sh"


class MuseGpuLockTests(unittest.TestCase):
    def run_recipe(self, name, expected_stages):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = root / "run_chains" / name
            chain.parent.mkdir()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            chain.write_text((ROOT / "run_chains" / name).read_text(encoding="utf-8").replace('PORT=8097','PORT='+str(port)), encoding="utf-8")
            (root / "run_chains/lib").mkdir()
            (root / "run_chains/lib/stage.sh").write_text(
                "stage_note() { :; }\n"
                "run_stage() { printf '%s\\n' \"$*\" >> \"$STAGE_CALLS\"; }\n"
                "stage_commit_artifacts() { :; }\n"
                "stage_summary() { :; }\n", encoding="utf-8")
            for helper_name in ('managed_server.sh','llm_campaign.sh','server_cleanup.sh'):
                shutil.copyfile(ROOT/'run_chains/lib'/helper_name,root/'run_chains/lib'/helper_name)
            (root/'app/env/bin').mkdir(parents=True)
            (root/'app/env/bin/python').symlink_to(sys.executable)
            for helper_name in ('llama_server_process.py','subprocess_ownership.py'):
                shutil.copyfile(ROOT/'app'/helper_name,root/'app'/helper_name)
            native=root/'cpu_listener.py';native.write_text('import http.server,os,pathlib,sys\npathlib.Path(os.environ["SERVER_STARTED"]).touch()\nhttp.server.HTTPServer(("127.0.0.1",int(sys.argv[sys.argv.index("--port")+1])),http.server.SimpleHTTPRequestHandler).serve_forever()\n')
            # A /health file makes this real CPU listener return HTTP 200.
            (root/'health').write_text('healthy fixture')
            model = root / "ab_test_runtime/models/muse-q3/Muse-Glimmer-30B-UD-Q3_K_XL.gguf"
            model.parent.mkdir(parents=True)
            model.write_text("model", encoding="utf-8")
            server = root / "llama-server"
            server.write_text('#!/bin/bash\nexec '+sys.executable+' '+str(native)+' "$@"\n',encoding='utf-8')
            server.chmod(0o755)
            wrapper = root / "gpu_job.sh"
            wrapper.write_text("#!/bin/bash\nif [ \"${1:-}\" = \"--check-lock-owner\" ]; then exec bash "+str(ROOT / "gpu_job.sh")+" \"$@\"; fi\nprintf '%s\\n' \"$*\" > \"$GPU_DISPATCH\"\n",
                               encoding="utf-8")
            wrapper.chmod(0o755)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for command_name in ("pkill", "sleep"):
                command = bin_dir / command_name
                command.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                command.chmod(0o755)
            env = {**os.environ, "LLAMA_BIN": str(server),
                   "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "GPU_DISPATCH": str(root / "dispatch.log"),
                   "SERVER_STARTED": str(root / "server_started"),
                   "STAGE_CALLS": str(root / "stages.log")}
            env.pop("ALEXANDRIA_GPU_LOCK_HELD", None)
            result = subprocess.run(["bash", str(chain)], env=env,
                                    capture_output=True, text=True, timeout=10, cwd=root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(name.removesuffix(".sh"), (root / "dispatch.log").read_text())
            self.assertFalse((root / "server_started").exists())
            env["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
            env.pop("ALEXANDRIA_GPU_LOCK_PID", None)
            env["GPU_LOCK"] = str(root / "fixture.lock")
            result = subprocess.run(["bash", str(chain)], env=env,
                                    capture_output=True, text=True, timeout=10, cwd=root)
            self.assertNotEqual(0, result.returncode, result.stderr)
            self.assertFalse((root / "server_started").exists())
            # A real CPU owner retains fd9 while the guarded command closes it.
            owner = ('exec 9>"$GPU_LOCK"; flock -x 9; '
                     'export ALEXANDRIA_GPU_LOCK_PID=$$; '
                     '"$@" 9>&-; rc=$?; exit "$rc"')
            result = subprocess.run(["bash", "-c", owner, "owned-fixture"] + ["bash", str(chain)],
                                    env=env, capture_output=True, text=True, timeout=10, cwd=root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "server_started").exists())
            stages = (root / "stages.log").read_text()
            self.assertEqual(expected_stages, len(stages.splitlines()))
            self.assertNotIn("gpu_job.sh", stages)

    def test_reasoning_recipe_queues_before_owned_server_and_requests(self):
        self.run_recipe("muse_local_reasoninglow_20260914.sh",4)

    def test_rest_recipe_queues_before_owned_server_and_requests(self):
        self.run_recipe("muse_local_rest_20260915.sh",3)


if __name__ == "__main__":
    unittest.main()
