import copy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

import numpy as np
import soundfile as sf
from tests.test_support import write_test_adapter


ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("evaluate_lora", ROOT / "tools" / "voice_lab" / "evaluate_lora.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


class LoraEvaluationTests(unittest.TestCase):
    def test_adapter_evaluation_generates_both_probes_and_scores_them(self):
        class FakeEngine:
            def generate_voice(self, output_path, **_kwargs):
                times = np.arange(8000, dtype=np.float32) / 16000
                sf.write(output_path, 0.1 * np.sin(2 * np.pi * 180 * times), 16000)
                return True

        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp, "voice")
            adapter_dir.mkdir()
            (adapter_dir / "adapter_model.safetensors").write_bytes(b"checkpoint")
            sf.write(adapter_dir / "ref_sample.wav", np.ones(8000) * 0.1, 16000)
            with (patch.object(evaluation, "get_speaker_similarity", return_value=0.9),
                  patch.object(evaluation, "apply_evaluation_seed") as apply_seed):
                result = evaluation.evaluate_adapter(
                    {"id": "voice"}, tmp, FakeEngine(), object(), "cpu")

        self.assertEqual("pass", result["status"])
        self.assertEqual(evaluation.EVALUATION_VERSION, result["version"])
        self.assertEqual(64, len(result["evidence"]["checkpoint_sha256"]))
        self.assertTrue(all(len(probe["audio_sha256"]) == 64
                            for probe in result["probes"]))
        self.assertEqual(["narration", "dialogue"],
                         [probe["id"] for probe in result["probes"]])
        self.assertTrue(all(probe["metrics"]["speaker_similarity"] == 0.9
                            for probe in result["probes"]))
        self.assertEqual(
            [evaluation.EVALUATION_SEED, evaluation.EVALUATION_SEED + 1],
            [probe["seed"] for probe in result["probes"]],
        )
        self.assertEqual(
            [call(evaluation.EVALUATION_SEED), call(evaluation.EVALUATION_SEED + 1)],
            apply_seed.call_args_list,
        )

    def test_audio_metrics_detect_silence_and_clipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "probe.wav"))
            audio = np.concatenate((np.zeros(8000), np.ones(4000), np.full(4000, 0.1)))
            sf.write(path, audio, 16000, subtype="FLOAT")

            metrics = evaluation.get_audio_metrics(path)

        self.assertAlmostEqual(1.0, metrics["duration_seconds"])
        self.assertAlmostEqual(0.5, metrics["silence_ratio"], places=3)
        self.assertAlmostEqual(0.25, metrics["clipping_ratio"], places=3)

    def test_warning_thresholds_are_warning_only(self):
        warnings = evaluation.get_warnings({
            "speaker_similarity": 0.2, "silence_ratio": 0.8, "clipping_ratio": 0.1,
        })
        self.assertEqual(["low_speaker_similarity", "excess_silence", "clipping"], warnings)

    def test_candidate_recommendation_prefers_quality_and_never_promotes(self):
        def result(similarity, warnings=None):
            return {"warnings": warnings or [], "probes": [{"metrics": {
                "speaker_similarity": similarity, "clipping_ratio": 0.0,
                "silence_ratio": 0.1,
            }}]}
        recommendation = evaluation.get_candidate_recommendation({
            "production": result(0.7, ["low_speaker_similarity"]),
            "epoch_001": result(0.8),
            "epoch_002": result(0.75),
        })
        self.assertEqual("epoch_001", recommendation["recommended"])
        self.assertTrue(recommendation["production_unchanged"])

    def test_candidate_cleanup_keeps_only_recommended_generated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "candidates")
            for name in ("epoch_001", "epoch_002"):
                Path(root, name).mkdir(parents=True)
            removed = evaluation.cleanup_candidates(tmp, "epoch_002", ["epoch_001", "epoch_002"])
            self.assertEqual(["epoch_001"], removed)
            self.assertFalse(Path(root, "epoch_001").exists())
            self.assertTrue(Path(root, "epoch_002").is_dir())

    def test_manifest_candidate_records_match_retained_recommendation(self):
        records = [{"id": "epoch_001"}, {"id": "epoch_002"}]

        self.assertEqual(
            [{"id": "epoch_002"}],
            evaluation.get_retained_candidate_records(records, "epoch_002"),
        )
        self.assertEqual(
            [], evaluation.get_retained_candidate_records(records, "production")
        )

    def test_checkpoint_partition_skips_production_and_candidate_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "voice")
            candidates = adapter / "candidates"
            write_test_adapter(adapter)
            for candidate_id, weights in (
                    ("epoch_001", 2.0),
                    ("epoch_002", 1.0),
                    ("epoch_003", 2.0)):
                candidate = candidates / candidate_id
                write_test_adapter(candidate, weights)
                sf.write(candidate / "ref_sample.wav", np.ones(80) * .1, 16000)

            production_hash, unique, duplicates, skipped = evaluation.partition_unique_candidates(
                str(adapter), str(candidates))

        self.assertEqual([], skipped)
        self.assertEqual(64, len(production_hash))
        self.assertEqual(["epoch_001"], [item[0] for item in unique])
        self.assertEqual(
            [("epoch_002", "production"), ("epoch_003", "epoch_001")],
            [(item["id"], item["duplicate_of"]) for item in duplicates],
        )

    def test_partition_preserves_invalid_candidates_and_dependency_errors_propagate(self):
        from adapter_artifacts import AdapterValidationDependencyError
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "voice")
            write_test_adapter(adapter)
            root = adapter / "candidates"
            for name in ("complete", "bad_config", "empty_reference"):
                candidate = root / name
                write_test_adapter(candidate, 2.0)
                sf.write(candidate / "ref_sample.wav", np.ones(80) * .1, 16000)
            (root / "bad_config" / "adapter_config.json").write_text("{}")
            sf.write(root / "empty_reference" / "ref_sample.wav", np.array([]), 16000)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            _hash, unique, duplicates, skipped = evaluation.partition_unique_candidates(
                str(adapter), str(root))
            self.assertEqual(["complete"], [item[0] for item in unique])
            self.assertEqual([], duplicates)
            self.assertEqual(["bad_config", "empty_reference"], [item["id"] for item in skipped])
            self.assertEqual(before, {p.relative_to(root): p.read_bytes()
                                     for p in root.rglob("*") if p.is_file()})
            with patch.object(evaluation, "validate_adapter_artifacts",
                              side_effect=AdapterValidationDependencyError("validator unavailable")):
                with self.assertRaisesRegex(AdapterValidationDependencyError, "unavailable"):
                    evaluation.partition_unique_candidates(str(adapter), str(root))

    def test_cleanup_uses_admitted_snapshot_and_preserves_later_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "candidates")
            for name in ("rejected", "selected", "training_started_later"):
                (root / name).mkdir(parents=True)
                (root / name / "marker").write_text(name)
            self.assertEqual(["rejected"], evaluation.cleanup_candidates(
                tmp, "selected", ["rejected", "selected"]))
            self.assertEqual("training_started_later",
                             (root / "training_started_later" / "marker").read_text())
            self.assertTrue((root / "selected").is_dir())

    def test_resume_requires_version_probes_and_audio_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "adapter_model.safetensors").write_bytes(b"checkpoint")
            Path(tmp, "ref_sample.wav").write_bytes(b"reference")
            result = {"version": evaluation.EVALUATION_VERSION, "probes": [],
                      "thresholds": evaluation.THRESHOLDS,
                      "evidence": {
                          "checkpoint_sha256": evaluation.get_file_sha256(
                              str(Path(tmp, "adapter_model.safetensors"))),
                          "reference_audio_sha256": evaluation.get_file_sha256(
                              str(Path(tmp, "ref_sample.wav"))),
                          "evaluation_spec_sha256": evaluation.get_evaluation_spec_sha256(
                              evaluation.PROBES, evaluation.EVALUATION_SEED,
                              evaluation.THRESHOLDS),
                      }}
            for index, (probe_id, text) in enumerate(evaluation.PROBES):
                filename = f"evaluation_{probe_id}.wav"
                Path(tmp, filename).write_bytes(b"audio")
                result["probes"].append({
                    "id": probe_id, "text": text, "seed": evaluation.EVALUATION_SEED + index,
                    "audio_file": filename, "audio_sha256": evaluation.get_file_sha256(str(Path(tmp, filename))),
                })
            self.assertTrue(evaluation.is_complete_evaluation(result, tmp))
            Path(tmp, "evaluation_dialogue.wav").unlink()
            self.assertFalse(evaluation.is_complete_evaluation(result, tmp))

    def test_resume_refuses_legacy_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "adapter_model.safetensors").write_bytes(b"checkpoint")
            Path(tmp, "ref_sample.wav").write_bytes(b"reference")
            legacy = {"version": 1, "probes": []}
            self.assertFalse(evaluation.is_complete_evaluation(legacy, tmp))


if __name__ == "__main__":
    unittest.main()


class EvaluationSpecIntegrityTests(unittest.TestCase):
    def test_edited_spec_with_unchanged_hash_is_rejected_from_actual_generated_evidence(self):
        from lora_evidence import get_evidence_error
        class Engine:
            def generate_voice(self, output_path, **kwargs):
                time = np.arange(8000, dtype=np.float32)/16000
                sf.write(output_path, .1*np.sin(2*np.pi*180*time), 16000)
                return True
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, 'voice')
            adapter.mkdir()
            (adapter/'adapter_model.safetensors').write_bytes(b'checkpoint')
            sf.write(adapter/'ref_sample.wav', np.ones(8000)*.1, 16000)
            with patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
                 patch.object(evaluation, 'apply_evaluation_seed'):
                valid = evaluation.evaluate_adapter({'id':'voice'}, tmp, Engine(), object(), 'cpu')
            expected = evaluation.get_evaluation_spec_sha256(
                evaluation.PROBES, evaluation.EVALUATION_SEED, evaluation.THRESHOLDS)
            self.assertIsNone(get_evidence_error(valid, str(adapter), expected))
            self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))
            files = {p.name: p.read_bytes() for p in adapter.iterdir()}
            mutations = (
                lambda r: r['probes'][0].update(text='Different sentence with unchanged WAV hash.'),
                lambda r: r['probes'][0].update(id='replacement'),
                lambda r: r['probes'].pop(),
                lambda r: r['probes'].reverse(),
                lambda r: r['probes'][0].update(seed=7),
                lambda r: r['probes'][1].update(seed=7),
                lambda r: r['thresholds'].update(speaker_similarity_min=-100),
                lambda r: r['probes'].append(copy.deepcopy(r['probes'][0])),
                lambda r: r['probes'].__setitem__(0, None),
                lambda r: r['probes'][0].pop('text'),
                lambda r: r.pop('thresholds'),
            )
            report_path = adapter/'evaluation.json'
            for index, mutate in enumerate(mutations):
                with self.subTest(index=index):
                    result = copy.deepcopy(valid)
                    mutate(result)
                    report_path.write_text(json.dumps(result))
                    before = report_path.read_bytes()
                    for requested in (None, expected):
                        self.assertIsNotNone(get_evidence_error(result, str(adapter), requested))
                    self.assertEqual(before, report_path.read_bytes())
                    for name, data in files.items():
                        self.assertEqual(data, (adapter/name).read_bytes())
            self.assertEqual(evaluation.PROBES[0][1], valid['probes'][0]['text'])


class EvaluationCacheShapeTests(unittest.TestCase):
    def make_valid_evidence(self, root):
        adapter = root / 'voice'
        adapter.mkdir()
        (adapter / 'adapter_model.safetensors').write_bytes(b'cache fixture checkpoint identity')
        sf.write(adapter / 'ref_sample.wav', np.ones(8000) * .1, 16000)

        class PcmEngine:
            def __init__(self, *args):
                pass

            def generate_voice(self, output_path, **kwargs):
                time = np.arange(8000, dtype=np.float32) / 16000
                sf.write(output_path, .1 * np.sin(2 * np.pi * 180 * time), 16000)
                return True

        with patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
             patch.object(evaluation, 'apply_evaluation_seed'):
            valid = evaluation.evaluate_adapter({'id':'voice'}, str(root), PcmEngine(), object(), 'cpu')
        self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))
        return adapter, valid, PcmEngine

    def test_malformed_json_probe_containers_are_incomplete_without_mutating_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, valid, _engine = self.make_valid_evidence(Path(tmp))
            files = {path.name: path.read_bytes() for path in adapter.iterdir()}
            cache = adapter / 'evaluation.json'
            for probes in (None, False, 7, 'narration', {'narration':{}},
                           [None], ['narration'], [{}], []):
                with self.subTest(probes=probes):
                    invalid = copy.deepcopy(valid)
                    invalid['probes'] = probes
                    cache.write_text(json.dumps(invalid))
                    before = cache.read_bytes()
                    self.assertFalse(evaluation.is_complete_evaluation(json.loads(before), str(adapter)))
                    self.assertEqual(before, cache.read_bytes())
                    for name, raw in files.items():
                        self.assertEqual(raw, (adapter / name).read_bytes())
            for invalid in (None, [], False, 7, 'evaluation'):
                with self.subTest(outer=invalid):
                    self.assertFalse(evaluation.is_complete_evaluation(invalid, str(adapter)))
            self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))

    def test_actual_evaluator_rebuilds_malformed_cache_then_resumes_verified_replacement(self):
        import contextlib
        import io
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, valid, engine_type = self.make_valid_evidence(root)
            cache = adapter / 'evaluation.json'
            invalid = copy.deepcopy(valid)
            invalid['probes'] = {'narration': {'old':True}}
            cache.write_text(json.dumps(invalid))
            before = cache.read_bytes()
            manifest = root / 'manifest.json'
            manifest.write_text('[{"id":"voice","name":"Fixture"}]')
            config = root / 'config.json'
            config.write_text('{}')
            package = ModuleType('speechbrain'); package.__path__ = []
            inference = ModuleType('speechbrain.inference'); inference.__path__ = []
            speaker = ModuleType('speechbrain.inference.speaker')
            speaker.EncoderClassifier = SimpleNamespace(from_hparams=Mock(return_value=SimpleNamespace(eval=Mock())))
            modules = {'torch': ModuleType('torch'), 'speechbrain':package,
                       'speechbrain.inference':inference,
                       'speechbrain.inference.speaker':speaker}
            argv = ['evaluate_lora.py', '--manifest', str(manifest), '--models-dir', str(root),
                    '--config', str(config), '--device', 'cpu']
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), \
                 patch.object(evaluation, 'resolve_device', return_value='cpu'), \
                 patch.object(evaluation, 'TTSEngine', engine_type), \
                 patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
                 patch.object(evaluation, 'apply_evaluation_seed'), \
                 patch.object(evaluation, 'evaluate_adapter', wraps=evaluation.evaluate_adapter) as evaluate, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(0, evaluation.main())
                evaluate.assert_called_once()
                self.assertNotEqual(before, cache.read_bytes())
                replacement = json.loads(cache.read_text())
                self.assertEqual(['narration','dialogue'], [item['id'] for item in replacement['probes']])
                self.assertTrue(evaluation.is_complete_evaluation(replacement, str(adapter)))
                self.assertEqual('complete', replacement['candidate_recommendation']['cleanup']['status'])
                self.assertEqual('production', json.loads(manifest.read_text())[0]['evaluation']['recommended_candidate'])
                evaluate.reset_mock()
                self.assertEqual(0, evaluation.main())
                evaluate.assert_not_called()
                self.assertEqual(replacement, json.loads(cache.read_text()))
            self.assertIn('EVALUATE voice', output.getvalue())
            self.assertIn('SKIP voice', output.getvalue())
            for probe in replacement['probes']:
                audio, sr = sf.read(adapter / probe['audio_file'])
                self.assertEqual(16000, sr)
                self.assertEqual(8000, len(audio))


class EvaluationProbeFreshnessTests(unittest.TestCase):
    def make_adapter(self, root):
        adapter = Path(root, "voice")
        adapter.mkdir()
        (adapter / "adapter_model.safetensors").write_bytes(b"checkpoint")
        sf.write(adapter / "ref_sample.wav", np.ones(8000) * .1, 16000)
        for probe_id, _text in evaluation.PROBES:
            sf.write(adapter / f"evaluation_{probe_id}.wav", np.ones(4000) * .2, 16000)
        return adapter

    def test_no_fresh_file_cannot_score_prior_audio_even_with_success_return(self):
        from unittest.mock import Mock
        for returned in (None, True, False):
            with self.subTest(returned=returned), tempfile.TemporaryDirectory() as tmp:
                adapter = self.make_adapter(tmp)
                dialogue = (adapter / "evaluation_dialogue.wav").read_bytes()
                engine = Mock()
                engine.generate_voice.return_value = returned
                with patch.object(evaluation, "apply_evaluation_seed"), \
                     patch.object(evaluation, "get_audio_metrics", wraps=evaluation.get_audio_metrics) as metrics, \
                     patch.object(evaluation, "get_speaker_similarity", return_value=.9) as similarity:
                    with self.assertRaisesRegex(RuntimeError, "probe generation failed: narration"):
                        evaluation.evaluate_adapter({"id": "voice"}, tmp, engine, object(), "cpu")
                self.assertEqual(1, engine.generate_voice.call_count)
                metrics.assert_not_called()
                similarity.assert_not_called()
                self.assertFalse((adapter / "evaluation_narration.wav").exists())
                self.assertEqual(dialogue, (adapter / "evaluation_dialogue.wav").read_bytes())

    def test_fresh_real_audio_accepts_legacy_none_and_hashes_new_probe_bytes(self):
        for returned in (None, True):
            with self.subTest(returned=returned), tempfile.TemporaryDirectory() as tmp:
                adapter = self.make_adapter(tmp)
                original = {p.name: p.read_bytes() for p in adapter.iterdir()}
                class Engine:
                    def generate_voice(self, output_path, **_kwargs):
                        self_test.assertFalse(Path(output_path).exists())
                        times = np.arange(8000, dtype=np.float32) / 16000
                        sf.write(output_path, .1 * np.sin(2 * np.pi * 180 * times), 16000)
                        return returned
                self_test = self
                with patch.object(evaluation, "apply_evaluation_seed"), \
                     patch.object(evaluation, "get_speaker_similarity", return_value=.9):
                    result = evaluation.evaluate_adapter({"id": "voice"}, tmp, Engine(), object(), "cpu")
                self.assertEqual("pass", result["status"])
                self.assertTrue(evaluation.is_complete_evaluation(result, str(adapter)))
                for probe in result["probes"]:
                    path = adapter / probe["audio_file"]
                    self.assertNotEqual(original[path.name], path.read_bytes())
                    self.assertEqual(evaluation.get_file_sha256(str(path)), probe["audio_sha256"])
                    self.assertAlmostEqual(.5, probe["metrics"]["duration_seconds"])
                for name in ("adapter_model.safetensors", "ref_sample.wav"):
                    self.assertEqual(original[name], (adapter / name).read_bytes())

    def test_explicit_failure_with_written_audio_and_empty_fresh_audio_are_rejected(self):
        for returned, empty in ((False, False), (None, True)):
            with self.subTest(returned=returned, empty=empty), tempfile.TemporaryDirectory() as tmp:
                adapter = self.make_adapter(tmp)
                class Engine:
                    def generate_voice(self, output_path, **_kwargs):
                        sf.write(output_path, np.zeros(0 if empty else 8000), 16000)
                        return returned
                with patch.object(evaluation, "apply_evaluation_seed"), \
                     patch.object(evaluation, "get_speaker_similarity") as similarity:
                    with self.assertRaises((RuntimeError, ValueError)):
                        evaluation.evaluate_adapter({"id": "voice"}, tmp, Engine(), object(), "cpu")
                similarity.assert_not_called()
                self.assertEqual(0 if empty else 8000, sf.info(adapter / "evaluation_narration.wav").frames)


class CandidateReferencePairingTests(unittest.TestCase):
    def test_candidate_uses_and_hashes_its_own_paired_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp, "voice")
            candidate = parent / "candidates" / "epoch_001"
            candidate.mkdir(parents=True)
            (parent / "adapter_model.safetensors").write_bytes(b"production")
            (candidate / "adapter_model.safetensors").write_bytes(b"candidate")
            sf.write(parent / "ref_sample.wav", np.ones(8000) * .2, 16000)
            sf.write(candidate / "ref_sample.wav", np.ones(8000) * .1, 16000)
            original = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
            class Engine:
                def generate_voice(self, output_path, voice_config, **_kwargs):
                    self_test.assertEqual(str(candidate), voice_config["_evaluation_"]["adapter_path"])
                    self_test.assertEqual(candidate, Path(output_path).parent)
                    times = np.arange(8000, dtype=np.float32) / 16000
                    sf.write(output_path, .1 * np.sin(2 * np.pi * 180 * times), 16000)
                    return True
            self_test = self
            with patch.object(evaluation, "apply_evaluation_seed"), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9) as similarity:
                result = evaluation.evaluate_adapter({"id": "voice"}, tmp, Engine(), object(), "cpu",
                                                     adapter_dir_override=str(candidate))
            self.assertTrue(all(args.args[1] == str(candidate / "ref_sample.wav")
                                for args in similarity.call_args_list))
            digest = evaluation.get_file_sha256(str(candidate / "ref_sample.wav"))
            self.assertEqual(digest, result["evidence"]["reference_audio_sha256"])
            self.assertNotEqual(evaluation.get_file_sha256(str(parent / "ref_sample.wav")), digest)
            self.assertTrue(evaluation.is_complete_evaluation(result, str(candidate)))
            self.assertFalse(evaluation.is_complete_evaluation(result, str(parent)))
            for name, data in original.items():
                self.assertEqual(data, (parent / name).read_bytes())

    def test_incomplete_candidate_cannot_borrow_parent_reference_before_inference(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp, "voice")
            candidate = parent / "candidates" / "epoch_001"
            candidate.mkdir(parents=True)
            (candidate / "adapter_model.safetensors").write_bytes(b"candidate")
            sf.write(parent / "ref_sample.wav", np.ones(8000) * .2, 16000)
            before = {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()}
            engine = Mock()
            with patch.object(evaluation, "apply_evaluation_seed") as seed:
                with self.assertRaisesRegex(FileNotFoundError, "missing ref_sample.wav"):
                    evaluation.evaluate_adapter({"id": "voice"}, tmp, engine, object(), "cpu",
                                                adapter_dir_override=str(candidate))
            seed.assert_not_called()
            engine.generate_voice.assert_not_called()
            self.assertEqual(before, {str(p.relative_to(parent)): p.read_bytes()
                                      for p in parent.rglob("*") if p.is_file()})


class EvaluationCleanupResumeTests(unittest.TestCase):
    def test_pending_or_malformed_cleanup_is_not_a_complete_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, valid, _engine = EvaluationCacheShapeTests.make_valid_evidence(self, Path(tmp))
            for cleanup in ({"status": "pending"}, {"status": "failed"}, {}, None, []):
                with self.subTest(cleanup=cleanup):
                    result = copy.deepcopy(valid)
                    result["candidate_recommendation"] = {"cleanup": cleanup}
                    before = copy.deepcopy(result)
                    self.assertFalse(evaluation.is_complete_evaluation(result, str(adapter)))
                    self.assertEqual(before, result)
            valid["candidate_recommendation"] = {"cleanup": {"status": "complete"}}
            self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))

    def test_actual_pending_cleanup_is_finished_and_next_run_skips(self):
        import contextlib
        import io
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, valid, engine_type = EvaluationCacheShapeTests.make_valid_evidence(self, root)
            candidate = adapter / "candidates" / "losing"
            candidate.mkdir(parents=True)
            weights = adapter / "adapter_model.safetensors"
            write_test_adapter(adapter)
            write_test_adapter(candidate)
            sf.write(candidate / "ref_sample.wav", np.ones(80) * .1, 16000)
            original_weights = weights.read_bytes()
            (candidate / weights.name).write_bytes(original_weights)
            (candidate / "partial-cleanup.txt").write_text("left by interruption")
            skipped_ids = ["unfinished", "missing_reference", "broken_weights", "broken_reference"]
            for name in skipped_ids:
                incomplete = adapter / "candidates" / name
                incomplete.mkdir(parents=True)
                if name != "unfinished":
                    write_test_adapter(incomplete)
                if name == "broken_weights":
                    (incomplete / weights.name).write_bytes(b"truncated")
                    sf.write(incomplete / "ref_sample.wav", np.ones(80) * .1, 16000)
                elif name == "broken_reference":
                    (incomplete / "ref_sample.wav").write_bytes(b"not audio")
            skipped_before = {str(p.relative_to(adapter)): p.read_bytes()
                              for name in skipped_ids
                              for p in (adapter / "candidates" / name).rglob("*") if p.is_file()}
            valid["candidate_recommendation"] = {
                "recommended": "production", "cleanup": {"status": "pending",
                    "planned_removals": ["losing"], "removed_candidates": []}}
            cache = adapter / "evaluation.json"
            cache.write_text(json.dumps(valid))
            before = cache.read_bytes()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([{"id": "voice", "name": "Fixture",
                "evaluation_candidates": [{"id": name} for name in ["losing", *skipped_ids]]}]))
            config = root / "config.json"
            config.write_text('{}')
            package = ModuleType("speechbrain"); package.__path__ = []
            inference = ModuleType("speechbrain.inference"); inference.__path__ = []
            speaker = ModuleType("speechbrain.inference.speaker")
            speaker.EncoderClassifier = SimpleNamespace(from_hparams=Mock(
                return_value=SimpleNamespace(eval=Mock())))
            modules = {"torch": ModuleType("torch"), "speechbrain": package,
                "speechbrain.inference": inference, "speechbrain.inference.speaker": speaker}
            argv = ["evaluate_lora.py", "--manifest", str(manifest), "--models-dir", str(root),
                "--config", str(config), "--device", "cpu"]
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(sys, "argv", argv), \
                 patch.object(evaluation, "resolve_device", return_value="cpu"), \
                 patch.object(evaluation, "TTSEngine", engine_type), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
                 patch.object(evaluation, "apply_evaluation_seed"), \
                 patch.object(evaluation, "evaluate_adapter", wraps=evaluation.evaluate_adapter) as evaluate, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(0, evaluation.main())
                evaluate.assert_called_once()
                self.assertFalse(candidate.exists())
                self.assertEqual(original_weights, weights.read_bytes())
                self.assertNotEqual(before, cache.read_bytes())
                completed = json.loads(cache.read_text())
                cleanup = completed["candidate_recommendation"]["cleanup"]
                self.assertEqual("complete", cleanup["status"])
                self.assertEqual(["losing"], cleanup["removed_candidates"])
                skipped = completed["candidate_recommendation"]["skipped_candidates"]
                self.assertEqual(sorted(skipped_ids), [item["id"] for item in skipped])
                self.assertTrue(all(item["status"] == "skipped_incomplete" and item["reason"]
                                    for item in skipped))
                self.assertTrue(all((adapter / "candidates" / name).is_dir() for name in skipped_ids))
                self.assertEqual(skipped_before, {str(p.relative_to(adapter)): p.read_bytes()
                    for name in skipped_ids
                    for p in (adapter / "candidates" / name).rglob("*") if p.is_file()})
                self.assertEqual(skipped_ids, [item["id"] for item in json.loads(manifest.read_text())[0]
                    ["evaluation_candidates"]])
                self.assertTrue(evaluation.is_complete_evaluation(completed, str(adapter)))
                self.assertEqual("production", json.loads(manifest.read_text())[0]
                    ["evaluation"]["recommended_candidate"])
                evaluate.reset_mock()
                self.assertEqual(0, evaluation.main())
                evaluate.assert_not_called()
                self.assertEqual(completed, json.loads(cache.read_text()))
                self.assertEqual(skipped_ids, [item["id"] for item in json.loads(manifest.read_text())[0]
                    ["evaluation_candidates"]])
            self.assertIn("incomplete, preserved:", output.getvalue())
            self.assertIn("EVALUATE voice", output.getvalue())
            self.assertIn("SKIP voice", output.getvalue())
