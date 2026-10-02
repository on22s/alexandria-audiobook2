"""Native private chains: real queue construction, exit statuses and Git state."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RegateFailureSummaryTests(unittest.TestCase):
    def write(self, root, name, text, executable=False):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)
        return path

    def prepare(self, root, chain_name):
        self.write(root, "app/env/bin/python", "#!/bin/bash\nexec " +
                   shlex.quote(sys.executable) + ' "$@"\n', True)
        for name in ("stage.sh", "server_cleanup.sh"):
            self.write(root, "run_chains/lib/" + name,
                       (ROOT / "run_chains/lib" / name).read_text())
        source = (ROOT / "run_chains" / chain_name).read_text()
        baseline = os.environ.get("REGATE_BASELINE_DIR")
        if baseline:
            source = (Path(baseline) / chain_name).read_text()
            source = source.replace(
                "REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git",
                "REPO=" + shlex.quote(str(root)))
        return self.write(root, "run_chains/" + chain_name, source)

    def run_chain(self, root, chain, **extra):
        return subprocess.run(["bash", str(chain)], cwd=root,
                              env={**os.environ, **extra}, text=True,
                              capture_output=True, timeout=15)

    def reference(self, root):
        chain = self.prepare(root, "regate_reference_text.sh")
        self.write(root, "lora_models/manifest.json", json.dumps([
            {"id": name, "sample_count": 200} for name in ("a", "b", "c")]))
        self.write(root, "ab_test_runtime/experiments/decontaminate_batch0.json",
                   json.dumps({"results": [{"adapter": name} for name in ("a", "b", "c")]}))
        for name in ("a", "b", "c"):
            self.write(root, f"ab_test_runtime/decontaminate/batch0/{name}/adapter/adapter_model.safetensors", "fixture")
            (root / f"ab_test_runtime/decontaminate/batch0/{name}/data").mkdir()
            self.write(root, f"ab_test_runtime/experiments/gate_promote__{name}.json", '{"previous":true}')
        self.write(root, "gpu_job.sh", '#!/bin/bash\nshift\nexec "$@"\n', True)
        self.write(root, "app/experiments/verify_adapter_identity.py", '''import json,os,sys
from pathlib import Path
args=sys.argv
name=Path(args[args.index('--adapter')+1]).parent.name
with open('events','a') as f:f.write(name+'\\n')
Path(args[args.index('--out')+1]).write_text(json.dumps({'measured':name}))
sys.exit(int(os.environ.get('GATE_RC','0')) if name=='a' else 0)
''')
        self.write(root, "promote_adapters.py", '''import os,sys
from pathlib import Path
assert sys.argv[1:]==['--dry-run']
with open('events','a') as f:f.write('preview\\n')
sys.exit(int(os.environ.get('PREVIEW_RC','0')))
''')
        return chain

    def test_all_gates_attempted_and_preview_cannot_hide_failure(self):
        for rc in (7, 3):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = self.run_chain(root, self.reference(root), GATE_RC=str(rc))
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn("REGATE COMPLETE", result.stdout)
                self.assertEqual(["a", "b", "c", "preview"], (root / "events").read_text().splitlines())
                if rc == 3:
                    self.assertIn("REJECTED a (measured identity verdict)", result.stdout)
                backups = list((root / "ab_test_runtime/experiments").glob("gate_reference_text_backup_*/gate_promote__a.json"))
                self.assertEqual(1, len(backups))
                self.assertEqual('{"previous":true}', backups[0].read_text())

    def test_preview_failure_and_success(self):
        for rc in (4, 0):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = self.run_chain(root, self.reference(root), PREVIEW_RC=str(rc))
                self.assertEqual(rc == 0, result.returncode == 0, result.stdout + result.stderr)
                self.assertEqual(rc == 0, "REGATE COMPLETE" in result.stdout)

    def test_unreadable_manifest_refuses_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = self.reference(root)
            (root / "lora_models/manifest.json").write_text("broken")
            result = self.run_chain(root, chain)
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse((root / "events").exists())

    def test_refresh_failure_preserves_head_and_index(self):
        for rc in (2, 7, 0):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chain = self.prepare(root, "regate_finish_20260818.sh")
                (root / "ab_test_runtime/logs/regate_finish").mkdir(parents=True)
                self.write(root, "bin/date", '#!/bin/bash\nif [ "$1" = -d ]; then exec /bin/date -d "+300 seconds" +%s; fi\nexec /bin/date "$@"\n', True)
                self.write(root, "bin/pgrep", "#!/bin/bash\nexit 1\n", True)
                self.write(root, "refresh_indexes.py", "import os,sys\nfrom pathlib import Path\nPath('RESULTS_INDEX.md').write_text('refreshed')\nsys.exit(int(os.environ['REFRESH_RC']))\n")
                for name in ("RESULTS_INDEX.md", "results_index.csv", "LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md", "ab_test_runtime/experiments/seed", "ab_test_runtime/audit/seed", "unrelated"):
                    self.write(root, name, "old")
                def git(*args):
                    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
                git("init", "-q")
                git("config", "user.email", "fixture@example.invalid")
                git("config", "user.name", "Fixture")
                git("add", ".")
                git("commit", "-qm", "initial")
                self.write(root, "unrelated", "staged")
                git("add", "unrelated")
                head, index = git("rev-parse", "HEAD"), git("write-tree")
                result = self.run_chain(root, chain, REFRESH_RC=str(rc), PATH=str(root / "bin") + ":" + os.environ["PATH"])
                self.assertEqual(rc == 0, result.returncode == 0, result.stdout + result.stderr)
                self.assertEqual("refreshed", (root / "RESULTS_INDEX.md").read_text())
                if rc:
                    self.assertEqual(head, git("rev-parse", "HEAD"))
                    self.assertEqual(index, git("write-tree"))
                else:
                    self.assertNotEqual(head, git("rev-parse", "HEAD"))
