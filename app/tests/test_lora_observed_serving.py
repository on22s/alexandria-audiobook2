"""Observed serving modes fence current claims without losing historical auditions."""
import json
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
import lora_evidence as evidence
from routers import lora
from tests import test_lora_candidate_promotion as promotion_fixture
from tests import test_lora_evaluation as evaluation_fixture
from tests.test_support import SyntheticEvaluationEngine, write_test_serving_adapter


class ObservedServingEvidenceTests(unittest.TestCase):
    def run_cli(self, root, engine_type, expected_return, *, overwrite=True):
        evaluation = evaluation_fixture.evaluation
        classifier = SimpleNamespace(from_hparams=Mock(return_value=SimpleNamespace(eval=Mock())))
        modules = {"torch": ModuleType("torch"), "speechbrain": ModuleType("speechbrain"),
                   "speechbrain.inference": ModuleType("speechbrain.inference"),
                   "speechbrain.inference.speaker": SimpleNamespace(EncoderClassifier=classifier)}
        argv = ["evaluate_lora.py", "--manifest", str(root / "manifest.json"),
                "--models-dir", str(root), "--config", str(root / "config.json"), "--device", "cpu"]
        if overwrite:
            argv.append("--overwrite")
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", argv), \
             patch.object(evaluation, "resolve_device", return_value="cpu"), \
             patch.object(evaluation, "TTSEngine", engine_type), \
             patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
             patch.object(evaluation, "apply_evaluation_seed"), contextlib.redirect_stdout(output):
            self.assertEqual(expected_return, evaluation.main(), output.getvalue())
        return output.getvalue()

    def test_unobserved_candidate_report_resumes_after_production_publication_failure(self):
        evaluation = evaluation_fixture.evaluation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, _valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(root)
            candidate = adapter / "candidates" / "epoch_002"
            write_test_serving_adapter(candidate, value=2.0)
            (root / "manifest.json").write_text(json.dumps([{
                "id": "voice", "evaluation_candidates": [{"id": "epoch_002"}]}]))
            (root / "config.json").write_text(json.dumps({"tts": {"mode": "mlx"}}))
            write_json = evaluation.atomic_json_write
            def interrupt_production(data, path, **kwargs):
                if Path(path) == adapter / "evaluation.json":
                    raise OSError("production publication interrupted")
                return write_json(data, path, **kwargs)
            with patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "atomic_json_write", side_effect=interrupt_production):
                self.run_cli(root, engine_type, 1, overwrite=False)
            self.assertFalse((adapter / "evaluation.json").exists())
            candidate_before = self.get_files(candidate)
            self.assertIn("evaluation.json", candidate_before)
            with patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "get_candidate_recommendation") as ranking, \
                 patch.object(evaluation, "cleanup_candidates") as cleanup:
                output = self.run_cli(root, engine_type, 0, overwrite=False)
                ranking.assert_not_called()
                cleanup.assert_not_called()
            self.assertIn("HISTORICAL RETAINED", output)
            self.assertEqual(candidate_before, self.get_files(candidate))
            result = json.loads((adapter / "evaluation.json").read_text())
            self.assertIsNone(evidence.get_evidence_error(result, str(adapter)))
            self.assertNotIn("serving_identity", result["evidence"])
            self.assertNotIn("recommended_candidate", json.loads((root / "manifest.json").read_text())[0]["evaluation"])
            before = self.get_files(root)
            with patch.object(engine_type, "get_lora_serving_identity", return_value=None):
                self.run_cli(root, engine_type, 0, overwrite=False)
            self.assertEqual(before, self.get_files(root))

    def test_failed_attempt_cleanup_does_not_leak_own_audio_with_malformed_report(self):
        evaluation = evaluation_fixture.evaluation
        for report in ("{not json", json.dumps({"probes": {"bad": {}}}),
                       json.dumps({"probes": [{}]}), json.dumps({"probes": [{"audio_file": []}]})):
            with self.subTest(report=report), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter, _valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(root)
                (adapter / "evaluation.json").write_text(report)
                (root / "manifest.json").write_text(json.dumps([{
                    "id": "voice", "evaluation": {"status": "pass", "recommended_candidate": "production"}}]))
                (root / "config.json").write_text("{}")
                before = self.get_files(root)
                with patch.object(evaluation, "partition_unique_candidates", side_effect=RuntimeError("partition interrupted")):
                    for _attempt in range(2):
                        self.run_cli(root, engine_type, 1)
                        self.assertEqual(before, self.get_files(root))

    def test_probe_cleanup_error_is_reported_without_aborting_remaining_adapters(self):
        evaluation = evaluation_fixture.evaluation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, _valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(root)
            write_test_serving_adapter(root / "voice2", value=2.0)
            (root / "manifest.json").write_text(json.dumps([{"id": "voice"}, {"id": "voice2"}]))
            (root / "config.json").write_text("{}")
            remove_audio = evaluation.remove_stale_audio
            locked = []
            def fail_one_removal(path):
                if Path(path).parent == adapter and not locked:
                    locked.append(path)
                    raise PermissionError("probe is locked")
                return remove_audio(path)
            with patch.object(evaluation, "partition_unique_candidates", side_effect=RuntimeError("partition interrupted")), \
                 patch.object(evaluation, "remove_stale_audio", side_effect=fail_one_removal):
                output = self.run_cli(root, engine_type, 1)
            self.assertEqual(2, output.count("EVALUATE "))
            self.assertIn("PROBE CLEANUP FAILED", output)
            self.assertEqual(1, len(locked))
            self.assertEqual([], list((root / "voice2").glob("evaluation_*.wav")))
            saved = json.loads((root / "manifest.json").read_text())
            self.assertTrue(all(entry["evaluation"]["status"] == "failed" for entry in saved))
            self.assertTrue(all("recommended_candidate" not in entry["evaluation"] for entry in saved))

    def test_cli_failure_cleanup_tracks_report_publication(self):
        evaluation = evaluation_fixture.evaluation
        for phase in ("partition", "ranking", "cleanup"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter, valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(root)
                candidate = adapter / "candidates" / "epoch_002"
                write_test_serving_adapter(candidate, value=2.0)
                (adapter / "evaluation.json").write_text(json.dumps(valid))
                manifest = root / "manifest.json"
                manifest.write_text(json.dumps([{
                    "id": "voice", "evaluation": {"status": "pass", "recommended_candidate": "epoch_002"},
                    "evaluation_candidates": [{"id": "epoch_002"}]}]))
                (root / "config.json").write_text("{}")
                before = self.get_files(root)
                original_cleanup = evaluation.cleanup_candidates
                def fail_after_cleanup(*args, **kwargs):
                    original_cleanup(*args, **kwargs)
                    raise RuntimeError("cleanup interrupted")
                target = {"partition": "partition_unique_candidates", "ranking": "get_candidate_recommendation",
                          "cleanup": "cleanup_candidates"}[phase]
                effect = fail_after_cleanup if phase == "cleanup" else RuntimeError("evaluation interrupted")
                with patch.object(evaluation, target, side_effect=effect):
                    self.run_cli(root, engine_type, 1)
                if phase == "partition":
                    self.assertEqual(before, self.get_files(root))
                else:
                    saved = json.loads(manifest.read_text())[0]["evaluation"]
                    self.assertEqual("failed", saved["status"])
                    self.assertNotIn("recommended_candidate", saved)
                    for checkpoint in (adapter, candidate):
                        if checkpoint.exists():
                            report = json.loads((checkpoint / "evaluation.json").read_text())
                            referenced = {probe["audio_file"] for probe in report["probes"]}
                            for audio in checkpoint.glob("evaluation_*.wav"):
                                relative = str(audio.relative_to(root))
                                self.assertTrue(relative in before or audio.name in referenced, str(audio))

    def test_http_promotion_survives_owned_claim_model_release(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        for scenario in ("matching", "unknown", "mismatch", "settings_changed", "engine_changed", "config_changed"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                models, manifest, adapter = self.make_comparison(tmp, identity)
                engine = SyntheticEvaluationEngine()
                import threading
                import tts
                from types import MethodType
                observed_identity = None if scenario == "unknown" else identity
                if scenario == "mismatch":
                    observed_identity = {**identity, "device": "cuda", "dtype": "bfloat16", "merge_mode": "unmerged"}
                engine._model_lock = threading.Lock()
                engine._local_lora_model = SimpleNamespace(_lora_serving_identity=observed_identity)
                engine._local_model_devices = {"lora": "cpu"}
                engine._lora_prompt_cache = {}
                engine.get_lora_serving_identity = MethodType(tts.TTSEngine.get_lora_serving_identity, engine)
                config = {}
                def release_models():
                    tts.TTSEngine._release_local_model_family(engine, "lora")
                    if scenario == "engine_changed":
                        manager.engine = SyntheticEvaluationEngine()
                    elif scenario == "settings_changed":
                        engine.get_model_settings = lambda: {"language": "French"}
                    elif scenario == "config_changed":
                        config["tts"] = {"language": "French"}
                    return True
                manager = SimpleNamespace(engine=engine, ensure_idle_local_models_released=Mock(
                    side_effect=release_models))
                state = copy.deepcopy(core.process_state)
                for row in state.values():
                    row["running"] = False
                for owner, key, value in ((core, "project_manager", manager),
                        (lora, "project_manager", manager), (core, "process_state", state),
                        (lora, "process_state", state), (core, "_task_claims", {}),
                        (core, "_gpu_leases", {}), (lora, "LORA_MODELS_DIR", str(models)),
                        (lora, "LORA_MODELS_MANIFEST", str(manifest))):
                    stack.enter_context(patch.object(owner, key, value))
                stack.enter_context(patch.object(lora, "load_app_config", return_value=config))
                stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
                stack.enter_context(patch.object(core, "llm_is_on_this_gpu", return_value=True))
                with lora.lock_adapter_naming(str(models), str(manifest)):
                    lora._get_user_adapter_path_locked(str(models), "voice")
                before = self.get_files(models)
                original = (adapter / "adapter_model.safetensors").read_bytes()
                expected = (adapter / "candidates/epoch_002/adapter_model.safetensors").read_bytes()
                app = FastAPI()
                app.include_router(lora.router)
                with TestClient(app) as client:
                    response = client.post("/api/lora/models/voice/promote",
                                           json={"expected_candidate_id": "epoch_002"})
                self.assertEqual(200 if scenario == "matching" else 409, response.status_code, response.text)
                manager.ensure_idle_local_models_released.assert_called_once()
                self.assertIsNone(engine.get_lora_serving_identity())
                self.assertFalse(state["lora_training"]["running"])
                self.assertEqual({}, core._task_claims)
                if scenario != "matching":
                    self.assertEqual(before, self.get_files(models))
                    continue
                self.assertEqual(expected, (adapter / "adapter_model.safetensors").read_bytes())
                backup = adapter / "promotion_backups" / response.json()["promotion"]["backup_id"]
                self.assertEqual(original, (backup / "adapter_model.safetensors").read_bytes())
                self.assertFalse(state["lora_training"]["running"])
                self.assertEqual({}, core._task_claims)

    def test_malformed_retained_records_refuse_without_changes(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, identity)
            rows = json.loads(manifest.read_text())
            rows[0]["evaluation_candidates"] = ["epoch_002"]
            manifest.write_text(json.dumps(rows))
            with patch.object(lora, "project_manager", SimpleNamespace(engine=SyntheticEvaluationEngine())), \
                 patch.object(lora, "load_app_config", return_value={}):
                before = self.get_files(models)
                for operation in (lora._get_lora_candidate_comparison, lora._promote_lora_candidate):
                    with self.assertRaises(HTTPException) as raised:
                        operation("voice", str(models), str(manifest))
                    self.assertEqual(409, raised.exception.status_code)
                self.assertEqual(before, self.get_files(models))

    def make_comparison(self, root, identity):
        models, manifest = promotion_fixture.LoraCandidatePromotionTests()._write_comparison_fixture(root)
        adapter = models / "voice"
        if identity is not None:
            for checkpoint in (adapter, adapter / "candidates" / "epoch_002"):
                path = checkpoint / "evaluation.json"
                result = json.loads(path.read_text())
                result["evidence"]["serving_identity"] = identity
                result["evidence"]["evaluation_spec_sha256"] = evidence.get_evaluation_spec_sha256(
                    [(probe["id"], probe["text"]) for probe in result["probes"]],
                    result["probes"][0]["seed"], result["thresholds"],
                    result["synthesis_settings"], serving_identity=identity)
                path.write_text(json.dumps(result))
        return models, manifest, adapter

    def get_files(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and not path.name.endswith(".lock")}

    def test_unsafe_recommendations_and_symlinks_refuse_without_mutation(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        for candidate_id in ("nested/epoch", "nested\\epoch", "..", {"id": "epoch_002"}):
            with self.subTest(candidate_id=candidate_id), tempfile.TemporaryDirectory() as tmp:
                models, manifest, adapter = self.make_comparison(tmp, identity)
                rows = json.loads(manifest.read_text())
                rows[0]["evaluation"]["recommended_candidate"] = candidate_id
                manifest.write_text(json.dumps(rows))
                with lora.lock_adapter_naming(str(models), str(manifest)):
                    lora._get_user_adapter_path_locked(str(models), "voice")
                before = self.get_files(models)
                with self.assertRaises(HTTPException) as raised:
                    lora._promote_lora_candidate("voice", str(models), str(manifest))
                self.assertEqual(409, raised.exception.status_code)
                self.assertEqual(before, self.get_files(models))
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, identity)
            with lora.lock_adapter_naming(str(models), str(manifest)):
                lora._get_user_adapter_path_locked(str(models), "voice")
            before = self.get_files(models)
            original_islink = lora.os.path.islink
            candidate_dir = str(adapter / "candidates" / "epoch_002")
            with patch.object(lora.os.path, "islink", side_effect=lambda path:
                    str(path) == candidate_dir or original_islink(path)):
                with self.assertRaises(HTTPException) as raised:
                    lora._promote_lora_candidate("voice", str(models), str(manifest))
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual(before, self.get_files(models))

    def test_identity_validation_refuses_malformed_records_and_copies_valid_records(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        invalid = [None, [], {}, {**identity, "policy_version": True},
                   {**identity, "policy_version": 1.0}, {**identity, "device": "cuda:0\nevil"},
                   {**identity, "merge_mode": []}, {**identity, "dtype": "bfloat16"},
                   {**identity, "extra": True}]
        for record in invalid:
            with self.subTest(record=record):
                self.assertIsNone(evidence.validate_serving_identity(record))
        validated = evidence.validate_serving_identity(identity)
        validated["device"] = "cuda"
        self.assertEqual("cpu", identity["device"])

    def test_old_evidence_is_auditionable_but_unknown_and_mismatched_modes_cannot_promote(self):
        cpu = SyntheticEvaluationEngine().get_lora_serving_identity()
        gpu = {**cpu, "device": "cuda", "dtype": "bfloat16", "merge_mode": "unmerged"}
        for recorded, observed in ((None, cpu), (cpu, None), (cpu, gpu)):
            with self.subTest(recorded=recorded, observed=observed), tempfile.TemporaryDirectory() as tmp:
                models, manifest, adapter = self.make_comparison(tmp, recorded)
                engine = SyntheticEvaluationEngine()
                with patch.object(engine, "get_lora_serving_identity", return_value=observed), \
                     patch.object(lora, "project_manager", SimpleNamespace(engine=engine)), \
                     patch.object(lora, "load_app_config", return_value={}):
                    before = self.get_files(models)
                    comparison = lora._get_lora_candidate_comparison("voice", str(models), str(manifest))
                    self.assertEqual("historical", comparison["evidence_state"])
                    self.assertEqual(1, len(comparison["probe_pairs"]))
                    self.assertIn("audio_url", comparison["probe_pairs"][0]["production"])
                    summary = lora._get_candidate_summary(json.loads(manifest.read_text())[0], str(adapter))
                    self.assertEqual("historical", summary["evidence_state"])
                    with self.assertRaises(HTTPException) as raised:
                        lora._promote_lora_candidate("voice", str(models), str(manifest))
                    self.assertEqual(409, raised.exception.status_code)
                    self.assertIn("historical", raised.exception.detail)
                    self.assertEqual(before, self.get_files(models))

    def test_matching_observed_mode_permits_real_promotion_and_retains_backup(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, identity)
            expected = (adapter / "candidates" / "epoch_002" / "adapter_model.safetensors").read_bytes()
            original = (adapter / "adapter_model.safetensors").read_bytes()
            with patch.object(lora, "project_manager", SimpleNamespace(engine=SyntheticEvaluationEngine())), \
                 patch.object(lora, "load_app_config", return_value={}):
                comparison = lora._get_lora_candidate_comparison("voice", str(models), str(manifest))
                self.assertEqual("current", comparison["evidence_state"])
                summary = lora._get_candidate_summary(json.loads(manifest.read_text())[0], str(adapter))
                self.assertEqual("mode_matched", summary["evidence_state"])
                self.assertEqual("candidate_recommended", summary["state"])
                promoted = lora._promote_lora_candidate("voice", str(models), str(manifest))
            self.assertEqual(expected, (adapter / "adapter_model.safetensors").read_bytes())
            self.assertEqual(original, (adapter / "promotion_backups" / promoted["backup_id"] /
                                        "adapter_model.safetensors").read_bytes())

    def test_explicit_expected_identity_enforces_matching_without_a_second_flag(self):
        cpu = SyntheticEvaluationEngine().get_lora_serving_identity()
        gpu = {**cpu, "device": "cuda", "dtype": "bfloat16", "merge_mode": "unmerged"}
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, cpu)
            result = json.loads((adapter / "evaluation.json").read_text())
            self.assertIn("changed", evidence.get_evidence_error(
                result, str(adapter), expected_serving_identity=gpu))

    def test_stale_engine_settings_and_tampered_identity_are_not_current(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, identity)
            engine = SyntheticEvaluationEngine({"tts": {"language": "French"}})
            with patch.object(lora, "project_manager", SimpleNamespace(engine=engine)), \
                 patch.object(lora, "load_app_config", return_value={}):
                comparison = lora._get_lora_candidate_comparison("voice", str(models), str(manifest))
                self.assertEqual("historical", comparison["evidence_state"])
                result_path = adapter / "evaluation.json"
                result = json.loads(result_path.read_text())
                result["evidence"]["serving_identity"]["device"] = "cuda"
                result_path.write_text(json.dumps(result))
                with self.assertRaises(HTTPException):
                    lora._get_lora_candidate_comparison("voice", str(models), str(manifest))

    def test_evaluator_refuses_mid_probe_serving_drift(self):
        cpu = SyntheticEvaluationEngine().get_lora_serving_identity()
        gpu = {**cpu, "device": "cuda", "dtype": "bfloat16", "merge_mode": "unmerged"}
        for observations in ((None, cpu), (cpu, None), (cpu, gpu), (cpu, cpu, gpu)):
            with self.subTest(observations=observations), tempfile.TemporaryDirectory() as tmp:
                adapter, valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(Path(tmp))
                report = adapter / "evaluation.json"
                report.write_text(json.dumps(valid))
                before = report.read_bytes()
                before_files = self.get_files(adapter)
                evaluation = evaluation_fixture.evaluation
                with patch.object(engine_type, "get_lora_serving_identity", side_effect=observations), \
                     patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
                     patch.object(evaluation, "apply_evaluation_seed"), \
                     self.assertRaisesRegex(ValueError, "serving identity"):
                    evaluation.evaluate_adapter({"id": "voice"}, tmp, engine_type(), object(), "cpu",
                                                allow_historical_replacement=True)
                self.assertEqual(before, report.read_bytes())
                self.assertEqual(before_files, self.get_files(adapter))

    def test_unobserved_replacement_requires_explicit_overwrite(self):
        evaluation = evaluation_fixture.evaluation
        with tempfile.TemporaryDirectory() as tmp:
            adapter, valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(Path(tmp))
            (adapter / "evaluation.json").write_text(json.dumps(valid))
            before = self.get_files(adapter)
            with patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9) as similarity, \
                 patch.object(evaluation, "apply_evaluation_seed"):
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    evaluation.evaluate_adapter({"id": "voice"}, tmp, engine_type(), object(), "cpu")
                self.assertEqual(before, self.get_files(adapter))
                similarity.assert_not_called()
                result = evaluation.evaluate_adapter({"id": "voice"}, tmp, engine_type(), object(), "cpu",
                                                     allow_historical_replacement=True)
                self.assertNotIn("serving_identity", result["evidence"])
                self.assertIsNone(evidence.get_evidence_error(result, str(adapter)))
                for name, content in before.items():
                    self.assertEqual(content, (adapter / name).read_bytes())

    def test_unknown_accessors_never_construct_or_query_runtime(self):
        class Engine:
            def get_lora_serving_identity(self):
                raise RuntimeError("observation unavailable")
        self.assertIsNone(evidence.get_engine_lora_serving_identity(Engine()))
        self.assertIsNone(evidence.get_engine_lora_serving_identity(object()))

    def test_comparison_hashes_each_evaluation_once(self):
        identity = SyntheticEvaluationEngine().get_lora_serving_identity()
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest, adapter = self.make_comparison(tmp, identity)
            with patch.object(lora, "project_manager", SimpleNamespace(engine=SyntheticEvaluationEngine())), \
                 patch.object(lora, "load_app_config", return_value={}), \
                 patch.object(lora, "get_evidence_error", wraps=evidence.get_evidence_error) as integrity:
                comparison = lora._get_lora_candidate_comparison("voice", str(models), str(manifest))
            self.assertEqual("current", comparison["evidence_state"])
            self.assertEqual(2, integrity.call_count)

    def test_missing_evaluation_and_engine_are_historical_without_settings_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lora, "project_manager", SimpleNamespace()), \
                 patch.object(lora, "load_app_config", return_value={}):
                summary = lora._get_candidate_summary({}, tmp)
            self.assertEqual("historical", summary["evidence_state"])
            self.assertIn("unavailable", summary["evidence_reason"])

    def test_unknown_evaluation_remains_integrity_valid_but_never_current(self):
        evaluation = evaluation_fixture.evaluation
        with tempfile.TemporaryDirectory() as tmp:
            adapter, valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(Path(tmp))
            with patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
                 patch.object(evaluation, "apply_evaluation_seed"):
                result = evaluation.evaluate_adapter({"id": "voice"}, tmp, engine_type(), object(), "cpu")
            self.assertNotIn("serving_identity", result["evidence"])
            self.assertIsNone(evidence.get_evidence_error(result, str(adapter)))
            self.assertFalse(evaluation.is_complete_evaluation(result, str(adapter)))

    def test_unobserved_cli_publishes_historical_probes_without_ranking_or_cleanup(self):
        evaluation = evaluation_fixture.evaluation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, valid, engine_type = evaluation_fixture.EvaluationCacheShapeTests().make_valid_evidence(root)
            candidate = adapter / "candidates" / "epoch_002"
            write_test_serving_adapter(candidate, value=2.0)
            original = (adapter / "adapter_model.safetensors").read_bytes()
            candidate_weights = (candidate / "adapter_model.safetensors").read_bytes()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([{
                "id": "voice", "evaluation_candidates": [{"id": "epoch_002"}]}]))
            config = root / "config.json"
            config.write_text(json.dumps({"tts": {"mode": "mlx"}}))
            package = ModuleType("speechbrain")
            package.__path__ = []
            inference = ModuleType("speechbrain.inference")
            inference.__path__ = []
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
                 patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
                 patch.object(evaluation, "apply_evaluation_seed"), \
                 patch.object(evaluation, "get_candidate_recommendation") as rank, \
                 patch.object(evaluation, "cleanup_candidates") as cleanup, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(0, evaluation.main())
                rank.assert_not_called()
                cleanup.assert_not_called()
            self.assertIn("HISTORICAL", output.getvalue())
            self.assertEqual(original, (adapter / "adapter_model.safetensors").read_bytes())
            self.assertEqual(candidate_weights, (candidate / "adapter_model.safetensors").read_bytes())
            for checkpoint in (adapter, candidate):
                result = json.loads((checkpoint / "evaluation.json").read_text())
                self.assertNotIn("serving_identity", result["evidence"])
                self.assertNotIn("candidate_recommendation", result)
                self.assertIsNone(evidence.get_evidence_error(result, str(checkpoint)))
            saved = json.loads(manifest.read_text())[0]
            self.assertEqual([{"id": "epoch_002"}], saved["evaluation_candidates"])
            self.assertNotIn("recommended_candidate", saved["evaluation"])
            with patch.object(lora, "load_app_config", return_value={}), \
                 patch.object(lora, "project_manager", SimpleNamespace(engine=engine_type())):
                summary = lora._get_candidate_summary(saved, str(adapter))
            self.assertEqual("awaiting_evaluation", summary["state"])
            saved["evaluation"]["recommended_candidate"] = "epoch_002"
            manifest.write_text(json.dumps([saved]))
            before = self.get_files(root)
            with patch.dict(sys.modules, modules), patch.object(sys, "argv", argv), \
                 patch.object(evaluation, "resolve_device", return_value="cpu"), \
                 patch.object(evaluation, "TTSEngine", engine_type), \
                 patch.object(engine_type, "get_lora_serving_identity", return_value=None), \
                 patch.object(evaluation, "get_speaker_similarity", return_value=.9), \
                 patch.object(evaluation, "apply_evaluation_seed"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, evaluation.main())
            self.assertEqual(before, self.get_files(root))
