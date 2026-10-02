import copy
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

    def test_manifest_validation_rejects_non_object_settings(self):
        manifest = _manifest()
        manifest["settings"] = []
        with self.assertRaisesRegex(ValueError, "settings must be an object"):
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


class BenchmarkEvidenceValidationTests(unittest.TestCase):
    def test_repetition_count_rejects_json_booleans(self):
        for count in (True, False, 1.5, '2', 0, -1):
            with self.subTest(count=count):
                manifest = _manifest()
                manifest['repetitions'] = count
                original = copy.deepcopy(manifest)
                with self.assertRaises(ValueError):
                    benchmark_core.validate_benchmark_manifest(manifest)
                self.assertEqual(original, manifest)
        for count in (1, 2):
            manifest = _manifest()
            manifest['repetitions'] = count
            self.assertEqual(count, benchmark_core.validate_benchmark_manifest(manifest)['repetitions'])

    def test_non_scalar_targets_raise_controlled_validation_errors(self):
        for target in (['local'], {'target': 'local'}, None, True, 1):
            with self.subTest(target=target):
                manifest = _manifest()
                manifest['targets'] = [target]
                original = copy.deepcopy(manifest)
                with self.assertRaises(ValueError):
                    benchmark_core.validate_benchmark_manifest(manifest)
                self.assertEqual(original, manifest)

    def test_fixture_ids_are_nonempty_unique_strings(self):
        for fixture_id in (1, True, ['sample'], {'id': 'sample'}, '', '   '):
            with self.subTest(fixture_id=fixture_id):
                manifest = _manifest()
                manifest['fixtures'][0]['id'] = fixture_id
                with self.assertRaises(ValueError):
                    benchmark_core.validate_benchmark_manifest(manifest)
        manifest = _manifest()
        manifest['fixtures'].append({'id': 'chunk-1', 'sha256': 'different'})
        original = copy.deepcopy(manifest)
        with self.assertRaises(ValueError):
            benchmark_core.validate_benchmark_manifest(manifest)
        self.assertEqual(original, manifest)
        manifest['fixtures'][1]['id'] = 'chunk-2'
        self.assertEqual(['chunk-1', 'chunk-2'], [f['id'] for f in
            benchmark_core.validate_benchmark_manifest(manifest)['fixtures']])

    def test_preflight_requires_evidence_for_each_requested_target(self):
        manifest = _manifest()
        for environments in ({}, {'local': _environment()},
                             {'local': _environment(), 'thunder': {}},
                             {'local': _environment(), 'thunder': None}):
            with self.subTest(environments=environments):
                original = copy.deepcopy(environments)
                with self.assertRaises(ValueError):
                    benchmark_core.get_benchmark_preflight_id(manifest, environments)
                self.assertEqual(original, environments)
        environments = {'local': _environment(), 'thunder': _environment('thunder')}
        first = benchmark_core.get_benchmark_preflight_id(manifest, environments)
        self.assertEqual(first, benchmark_core.get_benchmark_preflight_id(
            manifest, dict(reversed(list(environments.items())))))
        environments['thunder'] = _environment('thunder', 'GPU B')
        self.assertNotEqual(first, benchmark_core.get_benchmark_preflight_id(manifest, environments))
        manifest['targets'] = ['local']
        self.assertTrue(benchmark_core.get_benchmark_preflight_id(manifest, {'local': _environment()}))

    def test_edited_saved_evidence_is_rejected_without_rewriting_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'report.json')
            environment = _environment()
            original_environment = copy.deepcopy(environment)
            original_manifest = _manifest()
            valid = benchmark_core.build_benchmark_report(original_manifest, environment)
            valid['cases'].append({'fixture_id': 'chunk-1', 'status': 'passed'})
            mutations = (
                lambda r: r['manifest'].update(repetitions=2),
                lambda r: r['manifest']['fixtures'][0].update(sha256='edited'),
                lambda r: r['environment']['details'].update(gpu_name='GPU B'),
                lambda r: r['environment'].update(target='thunder'),
                lambda r: r.update(manifest=None),
                lambda r: r.update(environment=None),
                lambda r: r['environment'].update(details=[]),
                lambda r: r['environment'].update(target=['local']),
            )
            for index, mutate in enumerate(mutations):
                with self.subTest(index=index):
                    report = copy.deepcopy(valid)
                    mutate(report)
                    benchmark_core.save_benchmark_report(str(path), report)
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        benchmark_core.load_resumable_benchmark_report(
                            str(path), original_manifest, environment)
                    self.assertEqual(before, path.read_bytes())
                    self.assertEqual(original_environment, environment)
                    self.assertEqual(_manifest(), original_manifest)
            benchmark_core.save_benchmark_report(str(path), valid)
            before = path.read_bytes()
            self.assertEqual(valid, benchmark_core.load_resumable_benchmark_report(
                str(path), original_manifest, environment))
            self.assertEqual(before, path.read_bytes())
            forged_environment = copy.deepcopy(environment)
            forged_environment['details']['gpu_name'] = 'GPU B'
            with self.assertRaises(ValueError):
                benchmark_core.load_resumable_benchmark_report(
                    str(path), original_manifest, forged_environment)
