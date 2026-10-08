"""The cloud Qwen comparison must enter the shared GPU queue before loading."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "run_chains/cloud_tts_qwen_compare_20260824.py"


class CloudTtsGpuLockTests(unittest.TestCase):
    def test_model_load_waits_for_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run_chains/cloud_tts_qwen_compare_20260824.py"
            script.parent.mkdir()
            script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            import shutil
            shutil.copyfile(ROOT / "run_chains/cloud_comparison_provenance.py",
                            script.parent / "cloud_comparison_provenance.py")
            owner = root / "app/subprocess_ownership.py"
            owner.parent.mkdir()
            shutil.copyfile(ROOT / "app/subprocess_ownership.py", owner)

            build_dir = root / "ab_test_runtime/reference_spread"
            build_dir.mkdir(parents=True)
            (build_dir / "build_spread3.json").write_text(
                json.dumps({"ref_sample": "ref.wav", "ref_text": "reference"}), encoding="utf-8")
            wrapper = root / "gpu_job.sh"
            wrapper.write_text("#!/bin/bash\nif [ \"${1:-}\" = \"--check-lock-owner\" ]; then exec bash "+str(ROOT / "gpu_job.sh")+" \"$@\"; fi\nprintf '%s\\n' \"$*\" > \"$GPU_DISPATCH\"\n",
                               encoding="utf-8")
            wrapper.chmod(0o755)
            modules = root / "modules"
            modules.mkdir()
            (modules / "torch.py").write_text(
                "bfloat16 = object()\n__version__ = 'test'\n"
                "def manual_seed(seed): pass\n", encoding="utf-8")
            (modules / "soundfile.py").write_text(
                "def write(path, data, rate): open(path, 'wb').write(b'WAV')\n",
                encoding="utf-8")
            (modules / "qwen_tts.py").write_text(
                "import os\n"
                "class Qwen3TTSModel:\n"
                "    @classmethod\n"
                "    def from_pretrained(cls, *args, **kwargs):\n"
                "        open(os.environ['MODEL_LOADED'], 'w').write('yes')\n"
                "        return cls()\n"
                "    def generate_voice_clone(self, **kwargs): return [[0, 0]], 2\n",
                encoding="utf-8")
            snapshot = root / "hub/snapshots" / ("a" * 40)
            snapshot.mkdir(parents=True)
            (modules / "huggingface_hub.py").write_text(
                "class HfApi:\n"
                "    def model_info(self, *args, **kwargs):\n"
                "        return type('Info', (), {'sha': 'a'*40})()\n"
                "def snapshot_download(**kwargs): return " + repr(str(snapshot)) + "\n",
                encoding="utf-8")
            metadata_dir = modules / "qwen_tts-1.2.3.dist-info"
            metadata_dir.mkdir()
            (metadata_dir / "METADATA").write_text("Metadata-Version: 2.1\nName: qwen-tts\nVersion: 1.2.3\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            nvidia = bin_dir / "nvidia-smi"
            nvidia.write_text("#!/bin/bash\necho 'test gpu'\n", encoding="utf-8")
            nvidia.chmod(0o755)
            env = {**os.environ, "ROOT": str(root), "PYTHONPATH": str(modules),
                   "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "GPU_DISPATCH": str(root / "dispatch.log"),
                   "MODEL_LOADED": str(root / "model_loaded")}
            env.pop("ALEXANDRIA_GPU_LOCK_HELD", None)
            result = subprocess.run([sys.executable, str(script)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("cloud_tts_qwen_compare_20260824", (root / "dispatch.log").read_text())
            self.assertFalse((root / "model_loaded").exists())
            env["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
            env.pop("ALEXANDRIA_GPU_LOCK_PID", None)
            env["GPU_LOCK"] = str(root / "fixture.lock")
            result = subprocess.run([sys.executable, str(script)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, result.returncode, result.stderr)
            self.assertFalse((root / "model_loaded").exists())
            # A real CPU owner retains fd9 while the guarded command closes it.
            owner = ('exec 9>"$GPU_LOCK"; flock -x 9; '
                     'export ALEXANDRIA_GPU_LOCK_PID=$$; '
                     '"$@" 9>&-; rc=$?; exit "$rc"')
            result = subprocess.run(["bash", "-c", owner, "owned-fixture"] + [sys.executable, str(script)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "model_loaded").exists())
