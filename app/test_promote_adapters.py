import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import promote_adapters


class AdapterSourceTests(unittest.TestCase):
    def test_gate_path_wins_over_same_named_legacy_source(self):
        with tempfile.TemporaryDirectory() as root:
            legacy_root = Path(root, "legacy")
            decontam_root = Path(root, "decontaminate")
            Path(legacy_root, "voice", "adapter").mkdir(parents=True)
            gated = Path(decontam_root, "batch1", "voice", "adapter")
            gated.mkdir(parents=True)
            gates = Path(root, "gates")
            gates.mkdir()
            Path(gates, "gate_promote__voice.json").write_text(json.dumps({
                "adapter": str(gated), "median_ecapa": 0.7
            }), encoding="utf-8")
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "SOURCE", str(legacy_root)), \
                 patch.object(promote_adapters, "DECONTAMINATE_SOURCE",
                              str(decontam_root)):
                self.assertEqual(str(gated),
                                 promote_adapters.get_adapter_source("voice"))

    def test_resolves_one_decontamination_batch(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = Path(root, "batch4", "voice", "adapter")
            adapter.mkdir(parents=True)
            with patch.object(promote_adapters, "SOURCE", os.path.join(root, "legacy")), \
                 patch.object(promote_adapters, "DECONTAMINATE_SOURCE", root):
                self.assertEqual(str(adapter),
                                 promote_adapters.get_adapter_source("voice"))

    def test_refuses_ambiguous_decontamination_sources(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "batch1", "voice", "adapter").mkdir(parents=True)
            Path(root, "batch2", "voice", "adapter").mkdir(parents=True)
            with patch.object(promote_adapters, "SOURCE", os.path.join(root, "legacy")), \
                 patch.object(promote_adapters, "DECONTAMINATE_SOURCE", root):
                self.assertIsNone(promote_adapters.get_adapter_source("voice"))

    def test_installed_gate_score_overrides_stale_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            gates = Path(root, "gates")
            models = Path(root, "models")
            gates.mkdir()
            models.mkdir()
            Path(gates, "library_voice_fidelity_n10.json").write_text(
                json.dumps({"results": [{"adapter": "voice", "ecapa": 0.4}]}),
                encoding="utf-8")
            Path(models, "manifest.json").write_text(json.dumps([
                {"id": "voice", "gate_ecapa": 0.7}
            ]), encoding="utf-8")
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)):
                self.assertEqual(0.7, promote_adapters.shipped_scores()["voice"])

    def test_manifest_maps_training_num_samples_to_sample_count(self):
        with tempfile.TemporaryDirectory() as root:
            models = Path(root, "models")
            source = Path(root, "source")
            models.mkdir()
            source.mkdir()
            Path(models, "manifest.json").write_text(
                json.dumps([{"id": "voice", "sample_count": 200}]),
                encoding="utf-8")
            Path(source, "training_meta.json").write_text(
                json.dumps({"num_samples": 180}), encoding="utf-8")
            with patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(source)):
                promote_adapters.update_manifest({"voice": 0.7}, "stamp")
            manifest = json.loads(Path(models, "manifest.json").read_text())
            self.assertEqual(180, manifest[0]["sample_count"])


class GateVerdictTests(unittest.TestCase):
    """The promoter must obey the gate, not re-decide what the gate decided.

    `check` re-derived pass/fail from its own MIN_ECAPA and never read
    `passed`, while the gate computes `passed` against its own --min-ecapa.
    A gate run at a stricter threshold therefore reported FAIL and was
    promoted anyway - the exact opposite of what the module docstring
    promises. Every gate on disk happens to have used the default 0.45, so
    this never fired; that is what made it worth pinning.
    """

    def _gate(self, root, **fields):
        gates = Path(root, "gates")
        gates.mkdir(exist_ok=True)
        Path(gates, "gate_promote__voice.json").write_text(
            json.dumps(fields), encoding="utf-8")
        models = Path(root, "models", "voice")
        models.mkdir(parents=True, exist_ok=True)
        return gates, Path(root, "models")

    def test_a_failing_gate_is_refused_even_when_it_clears_min_ecapa(self):
        with tempfile.TemporaryDirectory() as root:
            gates, models = self._gate(
                root, median_ecapa=0.50, threshold=0.65, passed=False)
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(models)):
                ok, score, reason = promote_adapters.check("voice", {"voice": 0.40})
            # 0.50 clears MIN_ECAPA (0.45) and beats the shipped 0.40, so the
            # old threshold-only logic accepted it.
            self.assertGreater(score, promote_adapters.MIN_ECAPA)
            self.assertFalse(ok, "a gate that says FAIL must not be promoted")
            self.assertIn("FAIL", reason)

    def test_an_unfinished_identity_check_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            gates, models = self._gate(
                root, median_ecapa=0.70, threshold=0.45, passed=True,
                generation_failures=3)
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(models)):
                ok, _score, reason = promote_adapters.check("voice", {"voice": 0.40})
            self.assertFalse(ok, "a median over surviving lines is not the "
                                 "held-out evidence promotion claims")
            self.assertIn("generation failure", reason)

    def test_a_passing_gate_is_still_promoted(self):
        """The guard must not refuse what it was always meant to accept."""
        with tempfile.TemporaryDirectory() as root:
            gates, models = self._gate(
                root, median_ecapa=0.70, threshold=0.45, passed=True,
                generation_failures=0)
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(models)):
                ok, _score, _reason = promote_adapters.check("voice", {"voice": 0.40})
            self.assertTrue(ok)

    def test_a_gate_without_an_explicit_verdict_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            gates, models = self._gate(
                root, median_ecapa=0.70, threshold=0.45,
                generation_failures=0)
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(models)):
                ok, _score, reason = promote_adapters.check(
                    "voice", {"voice": 0.40})
            self.assertFalse(ok)
            self.assertIn("missing", reason)

    def test_reference_rank_campaign_selects_its_own_gate_prefix(self):
        original = promote_adapters.GATE_PREFIX
        try:
            with patch.object(sys, "argv", [
                    "promote_adapters.py", "--gate-campaign",
                    "reference-rank1", "--adapters", "voice"]), \
                 patch.object(promote_adapters, "promote", return_value=0) as run:
                self.assertEqual(0, promote_adapters.main())
            self.assertEqual("gate_reference_rank1__",
                             promote_adapters.GATE_PREFIX)
            run.assert_called_once()
        finally:
            promote_adapters.GATE_PREFIX = original


class GateCampaignTests(unittest.TestCase):
    """A campaign must not be addable in one place and not the other.

    The gate prefix lived in an if/else beside a separate argparse `choices`
    tuple. reference-rank2 was added to neither, so six rank-2 gates were
    written, one passed, and the promoter could not see any of them - it went
    on reading gate_promote__ and reported "no gate artifact" for adapters
    that had one. One table now feeds both.
    """

    def test_every_campaign_is_selectable_and_has_a_distinct_prefix(self):
        campaigns = promote_adapters.GATE_CAMPAIGNS
        self.assertIn("reference-rank2", campaigns)
        self.assertEqual(len(campaigns), len(set(campaigns.values())),
                         "two campaigns reading the same prefix would promote "
                         "each other's evidence")
        for prefix in campaigns.values():
            self.assertTrue(prefix.endswith("__"), prefix)

    def test_the_parser_offers_exactly_the_table(self):
        """argparse must not drift from the table it is meant to expose."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--gate-campaign",
                            choices=tuple(promote_adapters.GATE_CAMPAIGNS))
        for name in promote_adapters.GATE_CAMPAIGNS:
            self.assertEqual(name, parser.parse_args(
                [f"--gate-campaign={name}"]).gate_campaign)

    def test_the_rank2_retrain_directory_is_a_permitted_source(self):
        """A gate the promoter can read but whose adapter it cannot find is
        the same dead end one step later."""
        self.assertIn(promote_adapters.REFERENCE_RANK2_SOURCE,
                      promote_adapters.retrain_sources(),
                      "reference-rank2 resolves gates but its retrain "
                      "directory is not a permitted source, so promotion "
                      "would refuse everything it gated")

    def test_the_source_list_follows_patched_constants(self):
        """It was a module-level tuple, which snapshots the roots at import.

        That made it a second place the list lived, and it silently defeated
        every test that points the roots at a temporary directory - the real
        code kept resolving against the repo while the test thought it was
        sandboxed."""
        with patch.object(promote_adapters, "REFERENCE_RANK2_SOURCE", "/tmp/x"):
            self.assertIn("/tmp/x", promote_adapters.retrain_sources())


class RollbackRevertsManifestTests(unittest.TestCase):
    """A rollback that leaves the retrained score in the manifest sets a
    phantom baseline: `shipped_scores` prefers manifest `gate_ecapa`, and
    `check` refuses anything that does not beat it, so the next honest
    improvement gets rejected against a number the shipped weights lack."""

    def test_rollback_restores_the_pre_promotion_score(self):
        with tempfile.TemporaryDirectory() as root:
            models = Path(root, "models")
            backups = Path(root, "backups")
            Path(models, "voice").mkdir(parents=True)
            Path(models, "voice", "adapter_model.safetensors").write_text("new")
            Path(models, "manifest.json").write_text(json.dumps([
                {"id": "voice", "gate_ecapa": 0.71, "retrained_at": "stamp",
                 "sample_count": 120}]), encoding="utf-8")
            # the backup holds the originals
            backup = Path(backups, "stamp", "voice")
            backup.mkdir(parents=True)
            Path(backup, "adapter_model.safetensors").write_text("old")
            Path(backup, "training_meta.json").write_text(
                json.dumps({"num_samples": 200}), encoding="utf-8")
            Path(backups, "stamp.json").write_text(json.dumps({
                "promoted_at": "stamp",
                "adapters": [{"adapter": "voice", "gate_ecapa": 0.71,
                              "shipped_ecapa": 0.63}]}), encoding="utf-8")

            with patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "BACKUPS", str(backups)):
                promote_adapters.rollback("stamp")
                manifest = json.loads(Path(models, "manifest.json").read_text())
                self.assertEqual(0.63, manifest[0]["gate_ecapa"],
                                 "the baseline must go back to the score the "
                                 "restored weights actually earned")
                self.assertNotIn("retrained_at", manifest[0])
                self.assertEqual(200, manifest[0]["sample_count"],
                                 "training_meta fields come back too")
                self.assertEqual("old", Path(models, "voice",
                                             "adapter_model.safetensors").read_text())
                # and the restored baseline is what the next promotion sees
                self.assertEqual(0.63, promote_adapters.shipped_scores()["voice"])

    def test_a_missing_receipt_drops_the_score_rather_than_keeping_it(self):
        with tempfile.TemporaryDirectory() as root:
            models = Path(root, "models")
            backups = Path(root, "backups")
            Path(models, "voice").mkdir(parents=True)
            Path(models, "manifest.json").write_text(json.dumps([
                {"id": "voice", "gate_ecapa": 0.71}]), encoding="utf-8")
            Path(backups, "stamp", "voice").mkdir(parents=True)
            with patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "BACKUPS", str(backups)):
                promote_adapters.rollback("stamp")
            manifest = json.loads(Path(models, "manifest.json").read_text())
            self.assertNotIn("gate_ecapa", manifest[0],
                             "with no receipt, fall back to the measured "
                             "fidelity file rather than a stale score")


if __name__ == "__main__":
    unittest.main()
