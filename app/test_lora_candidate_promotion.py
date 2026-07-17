import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import lora


class LoraCandidatePromotionTests(unittest.TestCase):
    def _write_checkpoint(self, root, marker):
        Path(root).mkdir(parents=True, exist_ok=True)
        for filename in lora.PROMOTION_FILES:
            Path(root, filename).write_bytes(f"{marker}:{filename}".encode())

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

            self.assertEqual(b"candidate:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            self.assertFalse(candidate.exists())
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual("production", manifest[0]["evaluation"]["recommended_candidate"])
            self.assertEqual([], manifest[0]["evaluation_candidates"])
            self.assertEqual("promoted", promoted["status"])
            backup_dir = adapter / "promotion_backups" / promoted["backup_id"]
            self.assertTrue(backup_dir.is_dir())

            rolled_back = lora._rollback_lora_promotion(
                "voice", str(models), str(manifest_path))

            self.assertEqual(b"production:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
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
        for failed_position in range(1, len(lora.PROMOTION_FILES) + 1):
            with self.subTest(failed_position=failed_position), tempfile.TemporaryDirectory() as tmp:
                models = Path(tmp, "models")
                adapter = models / "voice"
                candidate = adapter / "candidates" / "epoch_002"
                manifest_path = models / "manifest.json"
                self._write_checkpoint(adapter, "production")
                self._write_checkpoint(candidate, "candidate")
                original_manifest = [{
                    "id": "voice",
                    "evaluation": {"recommended_candidate": "epoch_002"},
                    "evaluation_candidates": [{"id": "epoch_002"}],
                }]
                manifest_path.write_text(json.dumps(original_manifest))
                real_replace = lora.os.replace
                replace_count = 0

                def fail_selected_replace(source, destination):
                    nonlocal replace_count
                    if ".checkpoint_swap_staging" in str(source):
                        replace_count += 1
                        if replace_count == failed_position:
                            raise OSError("simulated replacement failure")
                    return real_replace(source, destination)

                with patch.object(lora.os, "replace", side_effect=fail_selected_replace):
                    with self.assertRaises(OSError):
                        lora._promote_lora_candidate("voice", str(models), str(manifest_path))

                self.assertEqual(b"production:adapter_config.json",
                                 (adapter / "adapter_config.json").read_bytes())
                self.assertTrue(candidate.is_dir())
                self.assertEqual(original_manifest, json.loads(manifest_path.read_text()))
                self.assertFalse((adapter / lora.CHECKPOINT_SWAP_JOURNAL).exists())

    def test_interrupted_swap_leaves_journal_and_explicit_recovery_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            candidate = adapter / "candidates" / "epoch_002"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(adapter, "production")
            self._write_checkpoint(candidate, "candidate")
            original_manifest = [{
                "id": "voice",
                "evaluation": {"recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
            }]
            manifest_path.write_text(json.dumps(original_manifest))
            real_replace = lora.os.replace

            def interrupt_replacement(source, destination):
                if ".checkpoint_swap_staging" in str(source):
                    raise KeyboardInterrupt()
                return real_replace(source, destination)

            with patch.object(lora.os, "replace", side_effect=interrupt_replacement):
                with self.assertRaises(KeyboardInterrupt):
                    lora._promote_lora_candidate("voice", str(models), str(manifest_path))

            self.assertTrue((adapter / lora.CHECKPOINT_SWAP_JOURNAL).is_file())
            self.assertEqual(original_manifest, json.loads(manifest_path.read_text()))
            recovered = lora._recover_checkpoint_swap(
                "voice", str(models), str(manifest_path))
            self.assertEqual("recovered", recovered["status"])
            self.assertEqual(b"production:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            self.assertFalse((adapter / lora.CHECKPOINT_SWAP_JOURNAL).exists())

    def test_failed_rollback_restores_promoted_checkpoint(self):
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
            lora._promote_lora_candidate("voice", str(models), str(manifest_path))
            real_replace = lora.os.replace

            def fail_rollback(source, destination):
                if ".checkpoint_swap_staging" in str(source):
                    raise OSError("simulated rollback failure")
                return real_replace(source, destination)

            with patch.object(lora.os, "replace", side_effect=fail_rollback):
                with self.assertRaises(OSError):
                    lora._rollback_lora_promotion("voice", str(models), str(manifest_path))

            self.assertEqual(b"candidate:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual("promoted", manifest[0]["promotion"]["status"])

    def test_interruption_before_manifest_save_recovers_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp, "models")
            adapter = models / "voice"
            candidate = adapter / "candidates" / "epoch_002"
            manifest_path = models / "manifest.json"
            self._write_checkpoint(adapter, "production")
            self._write_checkpoint(candidate, "candidate")
            original_manifest = [{
                "id": "voice",
                "evaluation": {"recommended_candidate": "epoch_002"},
                "evaluation_candidates": [{"id": "epoch_002"}],
            }]
            manifest_path.write_text(json.dumps(original_manifest))

            with patch.object(lora, "_save_manifest", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    lora._promote_lora_candidate("voice", str(models), str(manifest_path))

            self.assertEqual(b"candidate:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            self.assertTrue((adapter / lora.CHECKPOINT_SWAP_JOURNAL).is_file())
            self.assertTrue(candidate.is_dir())

            lora._recover_checkpoint_swap("voice", str(models), str(manifest_path))

            self.assertEqual(b"production:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            self.assertEqual(original_manifest, json.loads(manifest_path.read_text()))

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

    def test_failed_backup_deletion_keeps_manifest_reference(self):
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

            self.assertEqual(original_manifest, json.loads(manifest_path.read_text()))
            self.assertTrue(backup.is_dir())

    def _write_comparison_fixture(self, root, candidate_seed=42,
                                  candidate_audio="probe.wav"):
        models = Path(root, "models")
        adapter = models / "voice"
        candidate = adapter / "candidates" / "epoch_002"
        candidate.mkdir(parents=True)
        adapter.mkdir(parents=True, exist_ok=True)
        (adapter / "probe.wav").write_bytes(b"production")
        if candidate_audio == "probe.wav":
            (candidate / candidate_audio).write_bytes(b"candidate")
        probe = {"id": "neutral", "text": "Matched text", "seed": 42,
                 "audio_file": "probe.wav",
                 "metrics": {"speaker_similarity": 0.91}}
        (adapter / "evaluation.json").write_text(json.dumps({
            "probes": [probe],
            "candidate_recommendation": {
                "reason": "Candidate scored higher", "ranking": ["epoch_002"]},
        }))
        candidate_probe = {**probe, "seed": candidate_seed,
                           "audio_file": candidate_audio}
        (candidate / "evaluation.json").write_text(json.dumps({
            "probes": [candidate_probe],
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
            self.assertIn("audio is missing", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
