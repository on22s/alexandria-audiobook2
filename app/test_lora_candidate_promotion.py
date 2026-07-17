import json
from pathlib import Path
import tempfile
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

            rolled_back = lora._rollback_lora_promotion(
                "voice", str(models), str(manifest_path))

            self.assertEqual(b"production:adapter_config.json",
                             (adapter / "adapter_config.json").read_bytes())
            self.assertEqual("rolled_back", rolled_back["status"])

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


if __name__ == "__main__":
    unittest.main()
