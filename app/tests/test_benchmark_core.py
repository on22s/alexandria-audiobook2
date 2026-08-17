import tempfile
import unittest
from pathlib import Path

import benchmark_core


def _manifest():
    return {"schema_version": 1, "stage": "script_generation",
            "targets": ["local", "thunder"],
            "fixtures": [{"id": "chunk-1", "sha256": "abc"}]}


def _environment(target="local", gpu="GPU A"):
    return benchmark_core.build_environment_fingerprint(target, {
        "hostname": "host", "gpu_name": gpu, "backend": "rocm",
        "python_version": "3.10", "git_commit": "deadbeef"})


class BenchmarkCoreTests(unittest.TestCase):
    def test_stage_registry_is_copied_and_covers_voice_lab(self):
        stages = benchmark_core.get_stage_registry()
        stages["voicelab_training"]["gpu"] = False
        self.assertTrue(benchmark_core.STAGES["voicelab_training"]["gpu"])
        self.assertIn("voicelab_dedup", stages)
        self.assertFalse(stages["voicelab_naming"]["gpu"])

    def test_manifest_validation_normalizes_defaults_and_targets(self):
        manifest = _manifest()
        manifest["targets"].append("local")
        normalized = benchmark_core.validate_benchmark_manifest(manifest)
        self.assertEqual(["local", "thunder"], normalized["targets"])
        self.assertEqual(1, normalized["repetitions"])
        self.assertEqual({}, normalized["quality_thresholds"])

    def test_manifest_validation_rejects_unknown_stage_and_bad_fixture(self):
        manifest = _manifest()
        manifest["stage"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown benchmark stage"):
            benchmark_core.validate_benchmark_manifest(manifest)
        manifest = _manifest()
        manifest["fixtures"] = [{"id": "missing-hash"}]
        with self.assertRaisesRegex(ValueError, "requires id and sha256"):
            benchmark_core.validate_benchmark_manifest(manifest)

    def test_fingerprints_are_stable_and_change_with_environment(self):
        first = _environment()
        second = _environment()
        changed = _environment(gpu="GPU B")
        self.assertEqual(first, second)
        self.assertNotEqual(first["sha256"], changed["sha256"])

    def test_report_resume_requires_exact_manifest_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "report.json"))
            environment = _environment()
            report = benchmark_core.build_benchmark_report(_manifest(), environment)
            report["cases"].append({"fixture_id": "chunk-1", "status": "passed"})
            benchmark_core.save_benchmark_report(path, report)
            resumed = benchmark_core.load_resumable_benchmark_report(
                path, _manifest(), environment)
            self.assertEqual(1, len(resumed["cases"]))
            changed = _manifest()
            changed["repetitions"] = 2
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                benchmark_core.load_resumable_benchmark_report(path, changed, environment)
            with self.assertRaisesRegex(ValueError, "environment changed"):
                benchmark_core.load_resumable_benchmark_report(
                    path, _manifest(), _environment(gpu="GPU B"))


if __name__ == "__main__":
    unittest.main()
