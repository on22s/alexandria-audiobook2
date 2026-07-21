import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "ab_test_runtime", "run_runtime_ab.sh")
ANALYZER = os.path.join(ROOT, "ab_test_runtime", "analyze_runtime_ab.py")

spec = importlib.util.spec_from_file_location("runtime_ab_analyzer", ANALYZER)
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


class RuntimeAbAnalyzerTests(unittest.TestCase):
    def test_failed_runs_do_not_lower_completed_wall_mean(self):
        base = {"tok_per_sec": 5, "rescued": 0, "near_miss": 0, "recombined": 0}
        group = [{**base, "status": "complete", "wall_s": 100},
                 {**base, "status": "failed", "wall_s": 1}]
        self.assertEqual(100, analyzer.aggregate_group(group)["wall_s"])
    def test_malformed_passes_and_zero_metrics_do_not_crash_or_disappear(self):
        with tempfile.TemporaryDirectory() as rep:
            manifest = {"status": "complete", "passes": ["bad"],
                        "counts": {"near_miss_accepted": 0}}
            with open(os.path.join(rep, "book.threepass_manifest.json"), "w") as fh:
                json.dump(manifest, fh)
            row = analyzer.load_run(rep)
        self.assertIsNone(row["wall_s"])
        self.assertEqual(0, row["near_miss"])
        self.assertEqual("0.0", analyzer._display(0.0))

    def test_multiple_manifests_are_flagged_instead_of_chosen_arbitrarily(self):
        with tempfile.TemporaryDirectory() as rep:
            for name in ("a", "b"):
                with open(os.path.join(rep, name + ".threepass_manifest.json"), "w") as fh:
                    json.dump({"status": "complete", "passes": {}}, fh)
            row = analyzer.load_run(rep)
        self.assertEqual("ambiguous_manifest", row["status"])

    def test_legacy_resume_excludes_incomplete_wall_metric(self):
        with tempfile.TemporaryDirectory() as rep:
            manifest = {"status": "complete", "legacy_resume": True,
                        "passes": {"segment": {"elapsed_s": 1.0}}, "counts": {}}
            with open(os.path.join(rep, "book.threepass_manifest.json"), "w") as fh:
                json.dump(manifest, fh)
            row = analyzer.load_run(rep)
        self.assertIsNone(row["wall_s"])


class RuntimeAbRunnerTests(unittest.TestCase):
    def _write_executable(self, path, body):
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def test_runtime_selection_fails_when_selected_alias_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app")
            out = os.path.join(tmp, "out")
            os.mkdir(app)
            os.mkdir(out)
            fake_lms = os.path.join(tmp, "lms")
            self._write_executable(fake_lms, "#!/bin/sh\n"
                                   "if [ \"$1 $2\" = \"runtime ls\" ]; then echo '✓ wrong-runtime'; fi\n")
            command = f"source {RUNNER!r}; select_runtime wanted-runtime"
            env = {**os.environ, "APP": app, "OUT": out, "LMS": fake_lms,
                   "PY": sys.executable}
            result = subprocess.run(["bash", "-c", command], env=env,
                                    text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not match", result.stderr)

    def test_set_model_writes_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app")
            out = os.path.join(tmp, "out")
            os.mkdir(app)
            os.mkdir(out)
            with open(os.path.join(app, "config.json"), "w") as fh:
                json.dump({"llm_mode": "local", "llm": {
                    "model_name": "old", "base_url": "http://127.0.0.1:1234/v1"}}, fh)
            command = f"source {RUNNER!r}; set_model new-model"
            env = {**os.environ, "APP": app, "OUT": out, "LMS": "/bin/true",
                   "PY": sys.executable,
                   "PYTHONPATH": os.path.join(ROOT, "app")}
            subprocess.run(["bash", "-c", command], env=env, check=True)
            with open(os.path.join(app, "config.json")) as fh:
                config = json.load(fh)
        self.assertEqual("new-model", config["llm"]["model_name"])

    def test_set_model_rejects_remote_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app"); out = os.path.join(tmp, "out")
            os.mkdir(app); os.mkdir(out)
            with open(os.path.join(app, "config.json"), "w") as fh:
                json.dump({"llm_mode": "remote", "llm": {
                    "model_name": "old", "base_url": "https://remote/v1"}}, fh)
            env = {**os.environ, "APP": app, "OUT": out, "LMS": "/bin/true",
                   "PY": sys.executable, "PYTHONPATH": os.path.join(ROOT, "app")}
            result = subprocess.run(["bash", "-c", f"source {RUNNER!r}; set_model new"],
                                    env=env, text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("llm_mode=local", result.stderr)

    def test_failed_arm_is_counted_for_nonzero_final_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app"); out = os.path.join(tmp, "out")
            os.mkdir(app); os.mkdir(out); os.mkdir(os.path.join(app, "uploads"))
            with open(os.path.join(app, "config.json"), "w") as fh:
                json.dump({"llm_mode": "local", "llm": {
                    "model_name": "old", "base_url": "http://127.0.0.1:1234/v1"}}, fh)
            wrapper = os.path.join(tmp, "python-wrapper")
            self._write_executable(wrapper, "#!/bin/sh\n"
                                   f"if [ \"$1\" = - ]; then exec {sys.executable!r} \"$@\"; fi\n"
                                   "exit 7\n")
            env = {**os.environ, "APP": app, "OUT": out, "LMS": "/bin/true",
                   "PY": wrapper, "PYTHONPATH": os.path.join(ROOT, "app"), "REPEATS": "1"}
            command = f"source {RUNNER!r}; run_arm test tag model; test \"$RUN_FAILURES\" -eq 1"
            result = subprocess.run(["bash", "-c", command], env=env)
        self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
