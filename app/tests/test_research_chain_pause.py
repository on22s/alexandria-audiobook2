"""Native Bash checks of the research queue's pause boundary; no GPU jobs."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]


class ResearchChainPauseTests(unittest.TestCase):
    def run_queue_prefix(self, flag_kind, *, override=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_chains").mkdir()
            logs = root / "ab_test_runtime/logs"
            logs.mkdir(parents=True)
            marker = root / "worker-started"
            default_flag = logs / "gpu_paused"
            custom_flag = root / "custom-pause"
            legacy_flag = root / "ab_test_runtime/PAUSE_GPU_QUEUE"
            flag = {"default": default_flag, "custom": custom_flag,
                    "legacy": legacy_flag, "none": None}[flag_kind]
            if flag is not None:
                flag.write_text("held\n")
            source = (REPO / "run_chains/remaining_gpu_research.sh").read_text()
            prefix = source.split('pilot_out=', 1)[0]
            script = root / "run_chains/probe.sh"
            script.write_text(prefix + '\nstage fixture true\nexit $?\n')
            worker = root / "gpu_job.sh"
            worker.write_text('#!/bin/bash\ntouch "$FIXTURE_MARKER"\n')
            worker.chmod(0o755)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            sleeper = bin_dir / "sleep"
            sleeper.write_text('#!/bin/bash\n'
                'if [ -e "$FIXTURE_MARKER" ]; then exit 9; fi\n'
                'echo WAITED\nrm -- "$FIXTURE_FLAG"\n')
            sleeper.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                       FIXTURE_MARKER=str(marker), FIXTURE_FLAG=str(flag or custom_flag))
            env.pop("GPU_PAUSE_FLAG", None)
            if override:
                env["GPU_PAUSE_FLAG"] = str(custom_flag)
            result = subprocess.run(["bash", str(script)], env=env,
                                    capture_output=True, text=True, timeout=5)
            return result, marker.exists(), flag.exists() if flag else False

    def test_default_pause_waits_before_worker_and_resumes_after_release(self):
        result, started, retained = self.run_queue_prefix("default")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("WAITED", result.stdout)
        self.assertTrue(started)
        self.assertFalse(retained)

    def test_configured_pause_flag_uses_same_override_as_gpu_wrapper(self):
        result, started, retained = self.run_queue_prefix("custom", override=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("WAITED", result.stdout)
        self.assertTrue(started)
        self.assertFalse(retained)

    def test_legacy_hold_refuses_without_removing_flag_or_starting_worker(self):
        result, started, retained = self.run_queue_prefix("legacy")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("LEGACY PAUSE", result.stdout + result.stderr)
        self.assertFalse(started)
        self.assertTrue(retained)

    def test_unpaused_queue_starts_worker_without_wait(self):
        result, started, _ = self.run_queue_prefix("none")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("WAITED", result.stdout)
        self.assertTrue(started)

    def test_explicit_override_matches_wrapper_and_does_not_wait_on_default(self):
        result, started, retained = self.run_queue_prefix("default", override=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("WAITED", result.stdout)
        self.assertTrue(started)
        self.assertTrue(retained)

    def test_full_release_keeps_gpu_queue_and_rejects_full_only_skip(self):
        from verify_release import validate_api_summary

        source = (REPO / "run_chains/remaining_gpu_research.sh").read_text()
        self.assertIn("stage final_release_verification", source)
        self.assertIn('"$repo/app/verify_release.py" --full', source)
        summary = {
            "schema_version": 1, "mode": "full",
            "counts": {"passed": 0, "failed": 0, "skipped": 1, "total": 1},
            "tests": [{"name": "tts", "requires_full": True, "status": "skipped"}],
        }
        with self.assertRaisesRegex(ValueError, "Unexpected full API skips"):
            validate_api_summary(summary, True)
        summary["tests"][0]["status"] = "passed"
        summary["counts"].update(passed=1, skipped=0)
        self.assertEqual(summary["counts"], validate_api_summary(summary, True))
