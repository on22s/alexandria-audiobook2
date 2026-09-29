"""The reference re-gate chain must queue its model-loading step."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent.parent
CHAIN = ROOT / "run_chains/regate_reference_text.sh"


class RegateGpuLockTests(unittest.TestCase):
    def test_identity_gate_runs_through_gpu_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "app/env/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/bash\n'
                              'if [ "$1" = "-" ]; then printf "voice\\tadapter\\tdata\\n" > "$2"; fi\n'
                              'if [ "$1" = "-u" ]; then touch "$DIRECT_GATE"; fi\n',
                              encoding="utf-8")
            python.chmod(0o755)
            gpu_job = root / "gpu_job.sh"
            gpu_job.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$GPU_DISPATCH"\n',
                               encoding="utf-8")
            gpu_job.chmod(0o755)
            chain = root / "regate.sh"
            source = CHAIN.read_text(encoding="utf-8")
            source = source.replace(
                'REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                'REPO=' + shlex.quote(str(root)), 1)
            chain.write_text(source, encoding="utf-8")
            env = {**os.environ, "GPU_DISPATCH": str(root / "dispatch.log"),
                   "DIRECT_GATE": str(root / "direct.log")}
            result = subprocess.run(["bash", str(chain)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "dispatch.log").exists(), "GPU wrapper was bypassed")
            self.assertIn("regate_reference_text: voice", (root / "dispatch.log").read_text())
            self.assertFalse((root / "direct.log").exists())
