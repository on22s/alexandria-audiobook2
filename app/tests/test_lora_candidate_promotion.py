from tests.test_support import assert_file_lock_released
import asyncio
import multiprocessing
import os
import adapter_checkpoint_transaction as checkpoint_transaction
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import lora
from core import process_state
from lora_evidence import (EVALUATION_EVIDENCE_VERSION,
                           get_evaluation_spec_sha256, get_file_sha256)


def _write_real_checkpoint(root, marker):
    import numpy as np
    import soundfile as sf
    from tests.test_support import write_test_adapter
    root=Path(root);value={'production':1,'candidate':2,'older':3}.get(marker,4)
    write_test_adapter(root,value=value)
    sf.write(root/'ref_sample.wav',np.full(2400,value*.05,dtype=np.float32),24000)
    (root/'training_meta.json').write_text(json.dumps({'ref_sample_text':marker,
        'checkpoint_sha256':get_file_sha256(str(root/'adapter_model.safetensors')),
        'reference_audio_sha256':get_file_sha256(str(root/'ref_sample.wav'))}))
    (root/'README.md').write_text(marker)


def _checkpoint_bytes(marker):
    with tempfile.TemporaryDirectory() as tmp:
        _write_real_checkpoint(tmp,marker)
        return {name:(Path(tmp)/name).read_bytes() for name in lora.PROMOTION_FILES}


def _crash_http_checkpoint(models, operation, point):
    move,journal=checkpoint_transaction._move,checkpoint_transaction._save_journal
    adapter=Path(models)/'voice'
    def moved(source,target):
        move(source,target)
        if ((point=='original' and Path(target).name=='previous')
                or (point=='adapter' and Path(target)==adapter)
                or (point=='manifest' and Path(target)==Path(models)/'manifest.json')):os._exit(87)
    def recorded(root,data):
        journal(root,data)
        if point=='committed' and data['phase']=='committed':os._exit(87)
    with patch.object(checkpoint_transaction,'_move',side_effect=moved),patch.object(checkpoint_transaction,'_save_journal',side_effect=recorded):
        getattr(lora,operation)('voice',str(models),str(Path(models)/'manifest.json'))


def _run_crash(test,models,operation,point):
    child=multiprocessing.get_context('fork').Process(target=_crash_http_checkpoint,args=(models,operation,point))
    child.start();child.join(10)
    if child.is_alive():child.terminate();child.join(5);test.fail('owned HTTP checkpoint child did not finish')
    test.assertEqual(87,child.exitcode)


def _promotion_fixture(test,models):
    adapter=models/'voice';candidate=adapter/'candidates'/'epoch_002';manifest=models/'manifest.json'
    test._write_checkpoint(adapter,'production');test._write_checkpoint(candidate,'candidate')
    entries=[{'id':'voice','evaluation':{'recommended_candidate':'epoch_002'},'evaluation_candidates':[{'id':'epoch_002'}]}]
    manifest.write_text(json.dumps(entries))
    return adapter,candidate,manifest,entries


class LoraCandidatePromotionTests(unittest.TestCase):
    def _write_checkpoint(self, root, marker):
        _write_real_checkpoint(root,marker)

    def test_promotion_preserves_and_rolls_back_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            candidate = adapter / "candidates" / "epoch_002"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(adapter, "production")
            self._write_checkpoint(candidate, "candidate")
            manifest_path.write_text(json.dumps([{
                "id": "voice",
                "evaluation": {"recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
            }]))

            promoted = lora._promote_lora_candidate("voice", str(models), str(manifest_path))

            self.assertEqual(_checkpoint_bytes("candidate")["training_meta.json"],
                             (adapter / "training_meta.json").read_bytes())
            self.assertFalse(candidate.exists())
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual("production", manifest[0]["evaluation"]["recommended_candidate"])
            self.assertEqual([], manifest[0]["evaluation_candidates"])
            self.assertEqual("promoted", promoted["status"])
            backup_dir = adapter / "promotion_backups" / promoted["backup_id"]
            self.assertTrue(backup_dir.is_dir())

            rolled_back = lora._rollback_lora_promotion(
                "voice", str(models), str(manifest_path))

            self.assertEqual(_checkpoint_bytes("production")["training_meta.json"],
                             (adapter / "training_meta.json").read_bytes())
            self.assertEqual("rolled_back", rolled_back["status"])
            self.assertFalse(backup_dir.exists())
            manifest = json.loads(manifest_path.read_text())
            self.assertIsNone(manifest[0]["promotion"]["backup_id"])

    def test_promotion_refuses_production_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            models.mkdir()
            manifest_path = models / "manifest.json"
            manifest_path.write_text(json.dumps([{
                "id": "voice", "evaluation": {"recommended_candidate": "production"},
            }]))

            with self.assertRaises(HTTPException) as raised:
                lora._promote_lora_candidate("voice", str(models), str(manifest_path))

            self.assertEqual(409, raised.exception.status_code)

    def test_failed_replacement_restores_production_and_manifest(self):
        for point in ('before_journal','after_journal','original','adapter','manifest'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                models=Path(tmp)/'models';adapter,candidate,manifest,entries=_promotion_fixture(self,models)
                move,journal=checkpoint_transaction._move,checkpoint_transaction._save_journal;injected=[]
                def fail_once():
                    if not injected:injected.append(True);raise OSError('simulated native publication failure')
                def moved(source,target):
                    move(source,target)
                    if ((point=='original' and Path(target).name=='previous')
                            or (point=='adapter' and Path(target)==adapter)
                            or (point=='manifest' and Path(target)==manifest)):fail_once()
                def recorded(root,data):
                    if point=='before_journal' and data['phase']=='publishing':fail_once()
                    journal(root,data)
                    if point=='after_journal' and data['phase']=='publishing':fail_once()
                with patch.object(checkpoint_transaction,'_move',side_effect=moved),patch.object(checkpoint_transaction,'_save_journal',side_effect=recorded),self.assertRaises(OSError):
                    lora._promote_lora_candidate('voice',str(models),str(manifest))
                self.assertEqual([True],injected)
                self.assertEqual(_checkpoint_bytes('production'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
                self.assertTrue(candidate.is_dir());self.assertEqual(entries,json.loads(manifest.read_text()))
                self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())
                assert_file_lock_released(str(manifest))

    def test_interrupted_swap_leaves_journal_and_explicit_recovery_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp)/'models';adapter,candidate,manifest,entries=_promotion_fixture(self,models)
            _run_crash(self,models,'_promote_lora_candidate','original')
            self.assertFalse(adapter.exists())
            self.assertEqual([{'adapter_id':'voice','operation':'checkpoint_generation'}],lora.list_adapters_needing_recovery(str(models),str(manifest)))
            recovered=lora._recover_checkpoint_swap('voice',str(models),str(manifest))
            self.assertEqual('recovered',recovered['status'])
            self.assertEqual(_checkpoint_bytes('production'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
            self.assertEqual(entries,json.loads(manifest.read_text()));self.assertTrue(candidate.is_dir())
            self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())

    def test_failed_rollback_restores_promoted_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp)/'models';adapter,candidate,manifest,_=_promotion_fixture(self,models)
            lora._promote_lora_candidate('voice',str(models),str(manifest));original=manifest.read_bytes()
            move=checkpoint_transaction._move;injected=[]
            def moved(source,target):
                move(source,target)
                if Path(target)==adapter and not injected:injected.append(True);raise OSError('simulated rollback failure')
            with patch.object(checkpoint_transaction,'_move',side_effect=moved),self.assertRaises(OSError):
                lora._rollback_lora_promotion('voice',str(models),str(manifest))
            self.assertEqual(_checkpoint_bytes('candidate'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
            self.assertEqual(original,manifest.read_bytes())
            self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())

    def test_interruption_before_manifest_save_recovers_files_and_manifest(self):
        for point in ('adapter','manifest','committed'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                models=Path(tmp)/'models';adapter,candidate,manifest,entries=_promotion_fixture(self,models)
                _run_crash(self,models,'_promote_lora_candidate',point)
                self.assertEqual(_checkpoint_bytes('candidate'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
                self.assertTrue(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())
                lora._recover_checkpoint_swap('voice',str(models),str(manifest))
                marker='candidate' if point=='committed' else 'production'
                self.assertEqual(_checkpoint_bytes(marker),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
                if point=='committed':self.assertEqual('promoted',json.loads(manifest.read_text())[0]['promotion']['status'])
                else:self.assertEqual(entries,json.loads(manifest.read_text()))
                self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())

    def test_new_promotion_prunes_older_backup_after_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            candidate = adapter / "candidates" / "epoch_002"
            stale_backup = adapter / "promotion_backups" / "stale"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(adapter, "production")
            self._write_checkpoint(candidate, "candidate")
            self._write_checkpoint(stale_backup, "stale")
            manifest_path.write_text(json.dumps([{
                "id": "voice",
                "evaluation": {"recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
            }]))

            promoted = lora._promote_lora_candidate("voice", str(models), str(manifest_path))

            backups = sorted(path.name for path in (adapter / "promotion_backups").iterdir())
            self.assertEqual([promoted["backup_id"]], backups)

    def test_backup_status_and_explicit_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            backup = adapter / "promotion_backups" / "backup-1"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(backup, "backup")
            manifest_path.write_text(json.dumps([{
                "id": "voice",
                "promotion": {"status": "promoted", "backup_id": "backup-1",
                              "promoted_at": 123.0},
            }]))

            with patch.object(lora.shutil, "disk_usage",
                              return_value=SimpleNamespace(free=1024)):
                status = lora._get_lora_backup_status(str(models), str(manifest_path))

            self.assertTrue(status["low_space_warning"])
            self.assertEqual(1, len(status["backups"]))
            self.assertGreater(status["total_size_bytes"], 0)
            deleted = lora._delete_rollback_backup("voice", str(models), str(manifest_path))
            self.assertEqual("deleted", deleted["status"])
            self.assertFalse(backup.exists())
            manifest = json.loads(manifest_path.read_text())
            self.assertIsNone(manifest[0]["promotion"]["backup_id"])

    def test_backup_deletion_refuses_pending_checkpoint_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            backup = adapter / "promotion_backups" / "backup-1"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(backup, "backup")
            adapter.mkdir(parents=True, exist_ok=True)
            (adapter / lora.CHECKPOINT_SWAP_JOURNAL).write_text("{}")
            manifest_path.write_text(json.dumps([{
                "id": "voice",
                "promotion": {"status": "promoted", "backup_id": "backup-1"},
            }]))

            with self.assertRaises(HTTPException) as raised:
                lora._delete_rollback_backup("voice", str(models), str(manifest_path))

            self.assertEqual(409, raised.exception.status_code)
            self.assertTrue(backup.is_dir())

    def test_committed_backup_cleanup_failure_retains_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            backup = models / "voice" / "promotion_backups" / "backup-1"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(backup, "backup")
            original_manifest = [{
                "id": "voice",
                "promotion": {"status": "promoted", "backup_id": "backup-1"},
            }]
            manifest_path.write_text(json.dumps(original_manifest))

            with patch.object(lora.shutil, "rmtree", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    lora._delete_rollback_backup("voice", str(models), str(manifest_path))

            from adapter_publication import NAMING_PUBLICATION_JOURNAL
            import adapter_naming_transaction as naming
            self.assertIsNone(json.loads(manifest_path.read_text())[0]['promotion']['backup_id'])
            self.assertFalse(backup.exists())
            journal = json.loads((models / NAMING_PUBLICATION_JOURNAL).read_text())
            self.assertEqual('committed', journal['phase'])
            self.assertTrue((models / journal['workspace'] / 'adapters/0').is_dir())
            with naming.lock_adapter_naming(str(models), str(manifest_path)):
                self.assertTrue(naming.recover_adapter_naming_locked(str(models), str(manifest_path)))
                self.assertFalse(naming.recover_adapter_naming_locked(str(models), str(manifest_path)))
            self.assertFalse((models / NAMING_PUBLICATION_JOURNAL).exists())

    def _write_comparison_fixture(self, root, candidate_seed=42,
                                  candidate_audio="probe.wav"):
        models = Path(root, "models")
        adapter = models / "voice"
        candidate = adapter / "candidates" / "epoch_002"
        candidate.mkdir(parents=True)
        adapter.mkdir(parents=True, exist_ok=True)
        (adapter / "probe.wav").write_bytes(b"production")
        (adapter / "adapter_model.safetensors").write_bytes(b"production checkpoint")
        (adapter / "ref_sample.wav").write_bytes(b"production reference")
        (candidate / "adapter_model.safetensors").write_bytes(b"candidate checkpoint")
        (candidate / "ref_sample.wav").write_bytes(b"candidate reference")
        if candidate_audio == "probe.wav":
            (candidate / candidate_audio).write_bytes(b"candidate")
        probe = {"id": "neutral", "text": "Matched text", "seed": 42,
                 "audio_file": "probe.wav",
                 "metrics": {"speaker_similarity": 0.91}}
        probe["audio_sha256"] = get_file_sha256(str(adapter / "probe.wav"))
        spec_hash = get_evaluation_spec_sha256(
            (("neutral", "Matched text"),), 42, {})
        (adapter / "evaluation.json").write_text(json.dumps({
            "version": EVALUATION_EVIDENCE_VERSION,
            "evidence": {
                "checkpoint_sha256": get_file_sha256(
                    str(adapter / "adapter_model.safetensors")),
                "reference_audio_sha256": get_file_sha256(str(adapter / "ref_sample.wav")),
                "evaluation_spec_sha256": spec_hash,
            }, "thresholds": {}, "probes": [probe],
            "candidate_recommendation": {
                "reason": "Candidate scored higher", "ranking": ["epoch_002"]},
        }))
        candidate_probe = {**probe, "seed": candidate_seed,
                           "audio_file": candidate_audio}
        if candidate_audio == "probe.wav":
            candidate_probe["audio_sha256"] = get_file_sha256(str(candidate / candidate_audio))
        (candidate / "evaluation.json").write_text(json.dumps({
            "version": EVALUATION_EVIDENCE_VERSION,
            "evidence": {
                "checkpoint_sha256": get_file_sha256(
                    str(candidate / "adapter_model.safetensors")),
                "reference_audio_sha256": get_file_sha256(str(candidate / "ref_sample.wav")),
                "evaluation_spec_sha256": get_evaluation_spec_sha256(
                    (("neutral", "Matched text"),), candidate_seed, {}),
            }, "thresholds": {}, "probes": [candidate_probe],
        }))
        manifest = models / "manifest.json"
        manifest.write_text(json.dumps([{
            "id": "voice",
            "evaluation": {"recommended_candidate": "epoch_002"},
            "evaluation_candidates": [{"id": "epoch_002"}],
        }]))
        return models, manifest

    def test_comparison_returns_matched_audio_and_metrics_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest = self._write_comparison_fixture(tmp)
            original_manifest = manifest.read_text()

            result = lora._get_lora_candidate_comparison(
                "voice", str(models), str(manifest))

            self.assertTrue(result["advisory_only"])
            self.assertEqual("epoch_002", result["candidate_id"])
            self.assertEqual("Matched text", result["probe_pairs"][0]["text"])
            self.assertEqual(0.91, result["probe_pairs"][0]["production"]
                             ["metrics"]["speaker_similarity"])
            self.assertIn("/candidates/epoch_002/probe.wav",
                          result["probe_pairs"][0]["candidate"]["audio_url"])
            self.assertEqual(original_manifest, manifest.read_text())

    def test_comparison_refuses_mismatched_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest = self._write_comparison_fixture(tmp, candidate_seed=99)

            with self.assertRaises(HTTPException) as raised:
                lora._get_lora_candidate_comparison(
                    "voice", str(models), str(manifest))

            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("not comparable", raised.exception.detail)

    def test_comparison_refuses_missing_candidate_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest = self._write_comparison_fixture(
                tmp, candidate_audio="missing.wav")

            with self.assertRaises(HTTPException) as raised:
                lora._get_lora_candidate_comparison(
                    "voice", str(models), str(manifest))

            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("probe audio is invalid", raised.exception.detail)

    def test_comparison_refuses_evidence_after_checkpoint_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest = self._write_comparison_fixture(tmp)
            Path(models, "voice", "candidates", "epoch_002",
                 "adapter_model.safetensors").write_bytes(b"changed")

            with self.assertRaises(HTTPException) as raised:
                lora._get_lora_candidate_comparison(
                    "voice", str(models), str(manifest))

            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("does not match", raised.exception.detail)

    def test_candidate_summary_reports_lifecycle_counts_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "voice")
            adapter.mkdir()
            entry = {
                "evaluation": {"status": "pass", "recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
                "evaluation_candidate_skips": [
                    {"id": "epoch_001", "duplicate_of": "production"}],
            }
            original = json.loads(json.dumps(entry))
            (adapter / "evaluation.json").write_text(json.dumps({
                "candidate_recommendation": {
                    "candidate_metrics": {
                        "production": {"status": "pass"},
                        "epoch_001": {"status": "skipped_duplicate"},
                        "epoch_002": {"status": "pass"},
                    },
                    "cleanup": {"status": "complete"},
                },
            }))

            summary = lora._get_candidate_summary(entry, str(adapter))

            self.assertEqual("candidate_recommended", summary["state"])
            self.assertEqual(1, summary["evaluated_count"])
            self.assertEqual(1, summary["retained_count"])
            self.assertEqual(1, summary["duplicate_count"])
            self.assertTrue(summary["production_unchanged"])
            self.assertEqual("complete", summary["cleanup_status"])
            self.assertEqual(original, entry)

    def test_candidate_summary_survives_corrupt_evaluation_and_reports_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp, "voice")
            adapter.mkdir()
            (adapter / "evaluation.json").write_text("{broken")

            summary = lora._get_candidate_summary({
                "evaluation_candidates": "invalid",
                "promotion": {"status": "promoted"},
            }, str(adapter))

            self.assertEqual("promoted", summary["state"])
            self.assertEqual(0, summary["retained_count"])
            self.assertFalse(summary["production_unchanged"])


class LoraCheckpointSwapRouteClaimTests(unittest.TestCase):
    """Covers the Area 2 fix: the promote/rollback/recover routes must
    atomically claim the GPU lock (not just check-then-act), and must always
    release it, on both success and failure."""

    def _write_checkpoint(self, root, marker):
        _write_real_checkpoint(root,marker)

    def setUp(self):
        self._script_running = process_state["script"]["running"]
        self._lora_training_state = dict(process_state["lora_training"])

    def tearDown(self):
        process_state["script"]["running"] = self._script_running
        process_state["lora_training"].clear()
        process_state["lora_training"].update(self._lora_training_state)

    def test_promote_returns_400_while_another_gpu_task_is_running(self):
        process_state["script"]["running"] = True

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(lora.lora_promote_candidate("voice"))

        self.assertEqual(400, raised.exception.status_code)
        self.assertFalse(process_state["lora_training"]["running"])

    def test_running_flag_resets_after_successful_promote(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            candidate = adapter / "candidates" / "epoch_002"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(adapter, "production")
            self._write_checkpoint(candidate, "candidate")
            manifest_path.write_text(json.dumps([{
                "id": "voice",
                "evaluation": {"recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
            }]))

            with patch.object(lora, "LORA_MODELS_DIR", str(models)), \
                 patch.object(lora, "LORA_MODELS_MANIFEST", str(manifest_path)):
                result = asyncio.run(lora.lora_promote_candidate("voice"))

        self.assertEqual("promoted", result["status"])
        self.assertFalse(process_state["lora_training"]["running"])

    def test_running_flag_resets_after_promote_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            models.mkdir()
            manifest_path = models / "manifest.json"
            manifest_path.write_text(json.dumps([{
                "id": "voice", "evaluation": {"recommended_candidate": "production"},
            }]))

            with patch.object(lora, "LORA_MODELS_DIR", str(models)), \
                 patch.object(lora, "LORA_MODELS_MANIFEST", str(manifest_path)):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(lora.lora_promote_candidate("voice"))

        self.assertEqual(409, raised.exception.status_code)
        self.assertFalse(process_state["lora_training"]["running"])


if __name__ == "__main__":
    unittest.main()


class AlteredComparisonSpecTests(unittest.TestCase):
    def test_comparison_refuses_altered_probe_with_intact_audio_and_declared_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, manifest = LoraCandidatePromotionTests()._write_comparison_fixture(tmp)
            for path in (models/'voice/evaluation.json',
                         models/'voice/candidates/epoch_002/evaluation.json'):
                result = json.loads(path.read_text())
                result['probes'][0]['text'] = 'An edited comparison passage'
                path.write_text(json.dumps(result))
            before = {p: p.read_bytes() for p in models.rglob('*') if p.is_file()}
            with self.assertRaises(HTTPException) as raised:
                lora._get_lora_candidate_comparison('voice', str(models), str(manifest))
            self.assertEqual(409, raised.exception.status_code)
            self.assertIn('specification', raised.exception.detail)
            self.assertEqual({**before,models/'manifest.json.lock':b''}, {p: p.read_bytes() for p in models.rglob('*') if p.is_file()})


class PromotionCleanupFailureTests(unittest.TestCase):
    _write_checkpoint = LoraCandidatePromotionTests._write_checkpoint
    def test_committed_promotion_survives_cleanup_errors(self):
        for failed_cleanup in ("candidate", "backup", "both"):
            with self.subTest(failed_cleanup=failed_cleanup), tempfile.TemporaryDirectory() as tmp:
                models = Path(tmp, "models")
                adapter = models / "voice"
                candidate = adapter / "candidates" / "epoch_002"
                old_backup = adapter / "promotion_backups" / "old"
                manifest_path = models / "manifest.json"
                self._write_checkpoint(adapter, "production")
                self._write_checkpoint(candidate, "candidate")
                self._write_checkpoint(old_backup, "older")
                manifest_path.write_text(json.dumps([{
                    "id": "voice", "evaluation": {"recommended_candidate": "epoch_002"},
                    "evaluation_candidates": [{"id": "epoch_002"}],
                }]))
                original_rmtree = lora.shutil.rmtree
                attempts = []

                def fail_cleanup(path, *args, **kwargs):
                    target = Path(path)
                    if target in (candidate, old_backup):
                        # Both cleanup operations occur after publication and journal completion.
                        self.assertEqual("promoted", json.loads(manifest_path.read_text())[0]["promotion"]["status"])
                        self.assertFalse((adapter / lora.CHECKPOINT_SWAP_JOURNAL).exists())
                        attempts.append(target)
                        if ((target == candidate and failed_cleanup in ("candidate", "both")) or
                                (target == old_backup and failed_cleanup in ("backup", "both"))):
                            raise PermissionError("simulated cleanup denial")
                    return original_rmtree(path, *args, **kwargs)

                with patch.object(lora.shutil, "rmtree", side_effect=fail_cleanup), \
                     self.assertLogs(lora.logger, level="WARNING") as captured:
                    promoted = lora._promote_lora_candidate("voice", str(models), str(manifest_path))
                self.assertEqual([candidate, old_backup], attempts)
                self.assertEqual("promoted", promoted["status"])
                self.assertIn("cleanup", " ".join(captured.output).lower())
                self.assertIn("simulated cleanup denial", " ".join(captured.output))
                assert_file_lock_released(str(manifest_path))
                saved = json.loads(manifest_path.read_text())[0]
                self.assertEqual(promoted, saved["promotion"])
                self.assertEqual("production", saved["evaluation"]["recommended_candidate"])
                self.assertEqual([], saved["evaluation_candidates"])
                backup = adapter / "promotion_backups" / promoted["backup_id"]
                for filename in lora.PROMOTION_FILES:
                    self.assertEqual(_checkpoint_bytes("candidate")[filename], (adapter / filename).read_bytes())
                    self.assertEqual(_checkpoint_bytes("production")[filename], (backup / filename).read_bytes())
                self.assertEqual(failed_cleanup in ("candidate", "both"), candidate.exists())
                self.assertEqual(failed_cleanup in ("backup", "both"), old_backup.exists())
                # A committed promotion cannot be repeated; its retained backup can still restore it.
                with self.assertRaises(HTTPException) as raised:
                    lora._promote_lora_candidate("voice", str(models), str(manifest_path))
                self.assertEqual(409, raised.exception.status_code)
                rolled_back = lora._rollback_lora_promotion("voice", str(models), str(manifest_path))
                self.assertEqual("rolled_back", rolled_back["status"])
                for filename in lora.PROMOTION_FILES:
                    self.assertEqual(_checkpoint_bytes("production")[filename], (adapter / filename).read_bytes())

    def test_publication_failure_still_raises_and_restores_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp)/'models';adapter,candidate,manifest,_=_promotion_fixture(self,models);original=manifest.read_bytes()
            move=checkpoint_transaction._move
            def moved(source,target):
                if Path(target)==manifest:raise PermissionError('publication denied')
                move(source,target)
            with patch.object(checkpoint_transaction,'_move',side_effect=moved),self.assertRaisesRegex(PermissionError,'publication denied'):
                lora._promote_lora_candidate('voice',str(models),str(manifest))
            self.assertEqual(original,manifest.read_bytes());self.assertTrue(candidate.is_dir())
            self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())
            self.assertEqual(_checkpoint_bytes('production'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})


class NativeCheckpointHttpArtifactTests(unittest.TestCase):
    _write_checkpoint=staticmethod(_write_real_checkpoint)

    def test_real_http_promotion_rollback_and_recovery_preserve_artifacts_and_release_claim(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        for operation in ('promotion','rollback'):
            for point in ('adapter','manifest','committed'):
                with self.subTest(operation=operation,point=point),tempfile.TemporaryDirectory() as tmp:
                    models=Path(tmp)/'models';adapter,candidate,manifest,entries=_promotion_fixture(self,models)
                    app=FastAPI();app.include_router(lora.router)
                    running=process_state['lora_training']['running']
                    try:
                        process_state['lora_training']['running']=False
                        with patch.object(lora,'LORA_MODELS_DIR',str(models)),patch.object(lora,'LORA_MODELS_MANIFEST',str(manifest)),patch.object(lora,'project_manager',SimpleNamespace(engine=object())),TestClient(app) as client:
                            if operation=='rollback':
                                response=client.post('/api/lora/models/voice/promote');self.assertEqual(200,response.status_code)
                                self.assertEqual('promoted',response.json()['status']);self.assertFalse(process_state['lora_training']['running'])
                                entries=json.loads(manifest.read_text())
                            _run_crash(self,models,'_promote_lora_candidate' if operation=='promotion' else '_rollback_lora_promotion',point)
                            self.assertTrue(lora.list_adapters_needing_recovery(str(models),str(manifest)))
                            response=client.post('/api/lora/models/voice/recover-checkpoint-swap')
                            self.assertEqual(200,response.status_code);self.assertEqual('recovered',response.json()['status'])
                            self.assertFalse(process_state['lora_training']['running'])
                            self.assertEqual([],lora.list_adapters_needing_recovery(str(models),str(manifest)))
                            marker=('candidate' if operation=='promotion' else 'production') if point=='committed' else ('production' if operation=='promotion' else 'candidate')
                            self.assertEqual(_checkpoint_bytes(marker),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
                            if point!='committed':self.assertEqual(entries,json.loads(manifest.read_text()))
                            response=client.post('/api/lora/models/voice/recover-checkpoint-swap')
                            self.assertEqual(409,response.status_code);self.assertFalse(process_state['lora_training']['running'])
                            assert_file_lock_released(str(manifest))
                    finally:process_state['lora_training']['running']=running

    def test_legacy_pending_swap_recovers_through_native_transaction_and_retains_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp)/'models';adapter,candidate,manifest,entries=_promotion_fixture(self,models)
            backup=adapter/'promotion_backups'/'legacy';self._write_checkpoint(backup,'production')
            lora._copy_promotion_files(candidate,adapter)
            (adapter/lora.CHECKPOINT_SWAP_JOURNAL).write_text(json.dumps({'operation':'promotion','recovery_dir':'promotion_backups/legacy','manifest_entry':entries[0],'keep_recovery':True}))
            result=lora._recover_checkpoint_swap('voice',str(models),str(manifest))
            self.assertEqual('recovered',result['status'])
            self.assertEqual(_checkpoint_bytes('production'),{f:(adapter/f).read_bytes() for f in lora.PROMOTION_FILES})
            self.assertTrue(backup.is_dir());self.assertEqual(entries,json.loads(manifest.read_text()))
            self.assertFalse((adapter/lora.CHECKPOINT_SWAP_JOURNAL).exists())
            self.assertFalse(checkpoint_transaction.get_adapter_checkpoint_journal(adapter).exists())
