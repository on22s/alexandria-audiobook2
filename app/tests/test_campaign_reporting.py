"""Actual campaign scripts with CPU-only generator/server fixtures."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]


class CampaignReportingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ("run_chains/lib", "app/env/bin", "app/experiments", "bin",
                          "ab_test_runtime/logs", "ab_test_runtime/experiments",
                          "ab_test_runtime/distill/gguf/new_20260824"):
            (self.root / directory).mkdir(parents=True)
        for relative in ("run_chains/e_row_replication_20260818.sh",
                         "run_chains/unanswered_stratification_20260830.sh",
                         "run_chains/overnight_20260901.sh", "run_chains/lib/stage.sh", "run_chains/lib/server_cleanup.sh",
                         "run_chains/lib/llm_campaign.sh", "run_chains/lib/llm_job.sh", "run_chains/lib/reclaim_vram.sh"):
            shutil.copyfile(REPO / relative, self.root / relative)
            (self.root / relative).chmod(0o755)
        (self.root / "app/env/bin/python").symlink_to(sys.executable)
        for name in ('serving_results.py', 'serving_environment.py', 'manifest.py', 'respelling_completion.py'):
            shutil.copyfile(REPO / 'app/experiments' / name, self.root / 'app/experiments' / name)
        (self.root / 'app/lmstudio_settings.py').write_text("def get_lmstudio_status(model): return {}\n"
            "def get_current_status(mode,url,model): return dict(available=True,loaded=True,context_length=8192,parallel=2,optimized=None,runtime='llama.cpp')\n"
            "def get_gpu_name_and_backend(): return ('NVIDIA fixture GPU', 'cuda')\n")
        (self.root / "ab_test_runtime/distill/gguf/new_20260824/adapter_author_heldout_balanced.gguf").write_bytes(b"fixture")
        self._write("ensure_llama_server.sh", "#!/bin/sh\nexit 0\n")
        self._write("bin/pgrep", "#!/bin/sh\nexit 1\n")
        self._write("bin/rocm-smi", "#!/bin/sh\nexit 1\n")
        self._write("gpu_job.sh", "#!/bin/bash\nexec " + shlex.quote(sys.executable)
                    + ' "$(dirname "${BASH_SOURCE[0]}")/fixture_gpu.py" "$@"\n')
        self._write("fixture_gpu.py", "#!" + sys.executable + "\n" + "\n".join([
            "import json, os, pathlib, sys",
            "root = pathlib.Path(__file__).parent",
            "args = sys.argv[1:]",
            "if args[0] in ('--check-lock-owner', '--check-llm', '--check-vram'): raise SystemExit(0)",
            "with (root/'dispatch.log').open('a') as handle: handle.write(args[0] + '\\n')",
            "if args[0].startswith('unanswered_campaign_'):",
            "    os.environ.update(ALEXANDRIA_GPU_LOCK_HELD='1', ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()), ALEXANDRIA_GPU_LOCK_FD='9')",
            "    os.execvpe(args[1], args[1:], os.environ)",
            "rc = int(os.environ.get('FIXTURE_JOB_RC', '0')) if args[0].startswith('unanswered-') else 0",
            "if rc: raise SystemExit(rc)",
            "if '--out' in args:",
            "    out = pathlib.Path(args[args.index('--out') + 1])",
            "    limit = int(args[args.index('--limit') + 1]) if '--limit' in args else 0",
            "    doc = {'status': 'complete', 'limit': limit, 'candidates_considered':limit, 'run_identity':{'limit':limit}, 'results':[{'term':'term'+str(i)} for i in range(limit)]}",
            "else:",
            "    out = root/'ab_test_runtime/experiments'/('lora_serving_eval__'+args[0]+'.json')",
            "    doc = {'meta': {'validation': 'ok', 'finished': 1}, 'summary': {'base': {'n': 1, 'correct': 0}, 'lora': {'n': 1, 'correct': 1}}, 'rows': [dict(arm=a, id='index18:0', line='Hello', expected='ALICE', correct=(a == 'lora'), candidates=['ALICE'], in_candidates=True, predicted=('ALICE' if a == 'lora' else '')) for a in ('base', 'lora')]}",
            "out.parent.mkdir(parents=True, exist_ok=True)",
            "out.write_text(json.dumps(doc))",
        ]) + "\n")
        # Reporting fixture only; actual kernel admission is covered separately.
        self._write("app/llama_server_process.py", "raise SystemExit(0)\n")
        self._write("app/experiments/lora_serving_eval.py", "\n".join([
            "import json, os, pathlib, sys",
            "rc = int(os.environ.get('FIXTURE_JOB_RC', '0'))",
            "if rc: raise SystemExit(rc)",
            "root = pathlib.Path(__file__).parents[2]",
            "tag = sys.argv[sys.argv.index('--tag') + 1]",
            "(root/'captured_environment.json').write_text(os.environ['EXPERIMENT_ENV'])",
            "out = root/'ab_test_runtime/experiments'/('lora_serving_eval__'+tag+'.json')",
            "out.write_text(json.dumps({'meta': {'validation': 'ok', 'finished': 1}, 'summary': {'base': {'n': 1, 'correct': 0}, 'lora': {'n': 1, 'correct': 1}}, 'rows': [dict(arm=a, id='index18:0', line='Hello', expected='ALICE', correct=(a == 'lora'), candidates=['ALICE'], in_candidates=True, predicted=('ALICE' if a == 'lora' else '')) for a in ('base', 'lora')]}))",
        ]) + "\n")
        self._write("app/experiments/pair_e_row.py", "\n".join([
            "import json, sys",
            "with open(sys.argv[1]) as handle: artifact = json.load(handle)",
            "print('paired', artifact['limit'])",
        ]) + "\n")
        self.environment = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                                E_ROW_DEADLINE=str(int(time.time()) + 24000),
                                FIXTURE_JOB_RC="0")

    def _write(self, relative, source):
        path = self.root / relative
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, chain):
        return subprocess.run(["bash", str(self.root / "run_chains" / chain)],
                              cwd=self.root, env=self.environment, text=True,
                              capture_output=True, timeout=20)

    def test_e_row_scoring_creates_fresh_report_directory(self):
        report = self.root / "ab_test_runtime/reports/overnight_20260818/e_row_paired.txt"
        self.assertFalse(report.parent.exists())
        result = self._run("e_row_replication_20260818.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(report.exists(), result.stdout + result.stderr)
        self.assertEqual("paired 800\npaired 1200\npaired 1600\n", report.read_text())
        self.assertNotIn("scoring", result.stdout)

    def test_e_row_scoring_preserves_existing_report(self):
        report = self.root / "ab_test_runtime/reports/overnight_20260818/e_row_paired.txt"
        report.parent.mkdir(parents=True)
        report.write_text("prior evidence\n")
        result = self._run("e_row_replication_20260818.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("prior evidence\npaired 800\npaired 1200\npaired 1600\n", report.read_text())

    def test_stratification_preserves_failed_job_status(self):
        for rc in (7, 124):
            with self.subTest(rc=rc):
                self.environment["FIXTURE_JOB_RC"] = str(rc)
                result = self._run("unanswered_stratification_20260830.sh")
                self.assertEqual(rc, result.returncode, result.stdout + result.stderr)
                self.assertIn("FAILED", result.stdout)
                self.assertNotIn("COMPLETE", result.stdout)
                self.assertNotIn("no artifact to stratify", result.stdout)

    def test_stratification_success_still_scores_fixture_rows(self):
        result = self._run("unanswered_stratification_20260830.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("100.0% empty of   1", result.stdout)
        self.assertIn("  0.0% empty of   1", result.stdout)
        self.assertIn("COMPLETE", result.stdout)
        artifact = self.root / "ab_test_runtime/experiments/lora_serving_eval__unanswered-stratification-20260830.json"
        self.assertEqual(2, len(json.loads(artifact.read_text())["rows"]))

    def test_overnight_failed_first_stage_is_counted_and_later_stages_run(self):
        self.environment["FIXTURE_JOB_RC"] = "7"
        result = self._run("overnight_20260901.sh")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("refusal_stratification = failed:7", result.stdout)
        self.assertIn("3/4 stages ok", result.stdout)
        self.assertNotIn("COMPLETE overnight_20260901", result.stdout)
        self.assertEqual(4, len((self.root / "dispatch.log").read_text().splitlines()))

    def test_overnight_success_reports_all_four_stages(self):
        result = self._run("overnight_20260901.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("4/4 stages ok", result.stdout)
        self.assertIn("COMPLETE overnight_20260901", result.stdout)
        self.assertEqual(4, len((self.root / "dispatch.log").read_text().splitlines()))
