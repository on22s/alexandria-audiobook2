import os
import sys
import tempfile
import os
import unittest
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import promote_adapters
from tests.test_support import write_test_adapter


class FinitePromotionScoreTests(unittest.TestCase):
    INVALID = (float("nan"), float("inf"), float("-inf"),
               True, False, "0.8", [], {}, 10 ** 400)

    def _check(self, score, baseline, unseen=False):
        with tempfile.TemporaryDirectory() as root:
            gates = Path(root, "gates")
            gates.mkdir()
            models = Path(root, "models")
            (models / "voice").mkdir(parents=True)
            write_test_adapter(models)
            prefix = (promote_adapters.UNSEEN_PREFIX if unseen
                      else promote_adapters.GATE_CAMPAIGNS["promote"])
            suffix = "__clean" if unseen else ""
            (gates / f"{prefix}voice{suffix}.json").write_text(json.dumps({
                "median_ecapa": score, "passed": True,
                "generation_failures": 0,
            }))
            if unseen:
                (gates / f"{prefix}voice__shipped.json").write_text(
                    json.dumps({"median_ecapa": baseline}))
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "GATE_PREFIX", prefix), \
                 patch.object(promote_adapters, "get_adapter_source",
                              return_value=str(models)):
                return promote_adapters.check("voice", {"voice": baseline})

    def test_invalid_gate_scores_are_refused_without_exceptions(self):
        for score in self.INVALID:
            for unseen in (False, True):
                with self.subTest(score=score, unseen=unseen):
                    ok, returned_score, reason = self._check(score, 0.4, unseen)
                    self.assertFalse(ok)
                    self.assertIsNone(returned_score)
                    self.assertIn("finite", reason)

    def test_invalid_baselines_are_refused_without_exceptions(self):
        for score in self.INVALID:
            for unseen in (False, True):
                with self.subTest(score=score, unseen=unseen):
                    ok, returned_score, reason = self._check(0.8, score, unseen)
                    self.assertFalse(ok)
                    self.assertEqual(0.8, returned_score)
                    self.assertIn("finite", reason)

    def test_finite_scores_keep_threshold_and_improvement_guards(self):
        for score, baseline, expected in ((0.8, 0.4, True), (1, 0, True),
                                          (0.4, 0, False), (0.8, 0.8, False),
                                          (0.8, 0.9, False)):
            for unseen in (False, True):
                with self.subTest(score=score, baseline=baseline, unseen=unseen):
                    self.assertEqual(expected, self._check(score, baseline, unseen)[0])

    def test_nonfinite_gate_cannot_change_installed_files_or_write_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            models = root / "models"
            shipped = models / "voice"
            shipped.mkdir(parents=True)
            (shipped / "adapter_model.safetensors").write_bytes(b"shipped weights")
            manifest = models / "manifest.json"
            manifest.write_text(json.dumps([{"id": "voice", "gate_ecapa": 0.4}]))
            before = manifest.read_bytes()
            source = root / "source"
            source.mkdir()
            (source / "adapter_model.safetensors").write_bytes(b"candidate weights")
            gates = root / "gates"
            gates.mkdir()
            (gates / "gate_promote__voice.json").write_text(json.dumps({
                "median_ecapa": float("nan"), "passed": True,
                "adapter": str(source),
            }))
            backups = root / "backups"
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "BACKUPS", str(backups)), \
                 patch.object(promote_adapters, "GATE_PREFIX", "gate_promote__"), \
                 patch.object(promote_adapters, "shipped_scores", return_value={"voice": 0.4}), \
                 patch.object(promote_adapters, "get_adapter_source", return_value=str(source)):
                self.assertEqual(1, promote_adapters.promote(["voice"], "invalid", False))
            self.assertEqual(b"shipped weights", (shipped / "adapter_model.safetensors").read_bytes())
            self.assertEqual(before, manifest.read_bytes())
            self.assertFalse(backups.exists())


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
        write_test_adapter(Path(root, "models"))
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


class UnseenCampaignTests(unittest.TestCase):
    """The unseen campaign compares the two arms scored on the same clips
    neither adapter trained on; the shipped-score table is not consulted."""
    def _pair(self, root, clean, shipped, failures=0):
        gates = Path(root) / "gates"; gates.mkdir()
        models = Path(root) / "models"; (models / "voice").mkdir(parents=True)
        write_test_adapter(models)
        for arm, score in (("clean", clean), ("shipped", shipped)):
            (gates / f"unseen_gate__voice__{arm}.json").write_text(json.dumps({
                "adapter": "retrain/voice/adapter" if arm == "clean" else "lora_models/voice",
                "median_ecapa": score, "lines": 20, "threshold": 0.45,
                "generation_failures": failures if arm == "shipped" else 0,
                "passed": score >= 0.45}))
        return gates, models

    def _check(self, root, clean, shipped, failures=0):
        gates, models = self._pair(root, clean, shipped, failures)
        with patch.object(promote_adapters, "GATES", str(gates)), \
             patch.object(promote_adapters, "MODELS", str(models)), \
             patch.object(promote_adapters, "GATE_PREFIX", promote_adapters.UNSEEN_PREFIX), \
             patch.object(promote_adapters, "get_adapter_source", return_value=str(models)):
            # a rigged shipped score far above both arms must be ignored
            return promote_adapters.check("voice", {"voice": 0.99})

    def test_clean_beating_shipped_on_unseen_clips_is_promoted(self):
        with tempfile.TemporaryDirectory() as root:
            ok, score, reason = self._check(root, clean=0.6865, shipped=0.6576)
        self.assertTrue(ok, reason); self.assertEqual(0.6865, score); self.assertIn("0.658", reason)

    def test_clean_not_beating_shipped_on_unseen_clips_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            ok, _score, reason = self._check(root, clean=0.5693, shipped=0.5693)
        self.assertFalse(ok); self.assertIn("unseen", reason)

    def test_a_shipped_arm_with_generation_failures_blocks_the_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            ok, _score, reason = self._check(root, clean=0.70, shipped=0.50, failures=1)
        self.assertFalse(ok); self.assertIn("no shipped arm", reason)

    def test_discovery_yields_bare_names_from_the_clean_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            gates, _ = self._pair(root, 0.6, 0.5)
            with patch.object(promote_adapters, "GATES", str(gates)), \
                 patch.object(sys, "argv", ["promote_adapters.py", "--gate-campaign", "unseen", "--dry-run"]), \
                 patch.object(promote_adapters, "promote", return_value=0) as run:
                promote_adapters.main()
            run.assert_called_once(); self.assertEqual(["voice"], run.call_args[0][0])


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

    def test_the_goal27_small_retrain_directory_is_a_permitted_source(self):
        """Same dead end as rank 2, one retrain later: on 2026-09-28 three
        voices gated under goal27_small_20260928 beat their shipped scores
        (0.560->0.605, 0.581->0.631, 0.558->0.611) and were refused as "no
        retrained adapter on disk"."""
        self.assertIn(promote_adapters.GOAL27_SMALL_SOURCE,
                      promote_adapters.retrain_sources())

    def test_a_gated_adapter_under_goal27_small_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = os.path.join(tmp, "goal27_small_20260928", "v", "adapter")
            os.makedirs(adapter)
            with patch.object(promote_adapters, "GOAL27_SMALL_SOURCE",
                              os.path.dirname(os.path.dirname(adapter))), \
                 patch.object(promote_adapters, "gate_result",
                              return_value={"adapter": adapter}):
                self.assertEqual(os.path.realpath(adapter),
                                 promote_adapters.get_adapter_source("v"))

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

    def test_rollback_stages_every_backup_before_replacing_live_adapters(self):
        with tempfile.TemporaryDirectory() as root:
            models = Path(root, "models")
            backups = Path(root, "backups", "stamp")
            models.mkdir()
            backups.mkdir(parents=True)
            for name in ("first", "second"):
                Path(models, name).mkdir()
                Path(models, name, "weights").write_text("live " + name)
                Path(backups, name).mkdir()
                Path(backups, name, "weights").write_text("backup " + name)
            original_copy = promote_adapters.shutil.copytree

            def fail_second(source, target, *args, **kwargs):
                if Path(source).name == "second":
                    raise OSError("backup unreadable")
                return original_copy(source, target, *args, **kwargs)

            with patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "BACKUPS", str(backups.parent)), \
                 patch.object(promote_adapters.shutil, "copytree", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "backup unreadable"):
                    promote_adapters.rollback("stamp")
            for name in ("first", "second"):
                self.assertEqual("live " + name, Path(models, name, "weights").read_text())

    def test_rollback_rejects_non_directory_backup_before_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            models = Path(root, "models")
            backups = Path(root, "backups", "stamp")
            Path(models, "voice").mkdir(parents=True)
            Path(models, "voice", "weights").write_text("live")
            backups.mkdir(parents=True)
            Path(backups, "voice").write_text("broken")
            with patch.object(promote_adapters, "MODELS", str(models)), \
                 patch.object(promote_adapters, "BACKUPS", str(backups.parent)):
                with self.assertRaisesRegex(ValueError, "invalid rollback adapter"):
                    promote_adapters.rollback("stamp")
            self.assertEqual("live", Path(models, "voice", "weights").read_text())


if __name__ == "__main__":
    unittest.main()


class EcapaPairPathTest(unittest.TestCase):
    """Paths must survive the directory change this call makes.

    ecapa_pairs runs its subprocess with cwd=APP, so a relative path the
    caller handed in is re-resolved against app/. On 2026-08-18 the re-gate
    chain passed --dataset ab_test_runtime/decontaminate/... from the repo
    root and all 67 adapters failed identically with "System error" opening
    clips that were present and readable the whole time - app/ab_test_runtime/
    was what did not exist. Two GPU hours, zero measurements, and the printed
    advice ("check the paths") pointed at the datasets rather than at the cwd.
    """

    def _sent(self, pairs, cwd):
        from experiments import library_voice_fidelity as lvf
        with patch("subprocess.run") as run, patch("os.path.exists",
                                                   return_value=True):
            run.return_value.returncode = 0
            run.return_value.stdout = "[0.9]"
            with patch("os.getcwd", return_value=cwd):
                lvf.ecapa_pairs(pairs, "/usr/bin/python3")
            return json.loads(run.call_args.kwargs["input"])

    def test_relative_pairs_are_made_absolute_before_the_subprocess(self):
        sent = self._sent([["data/a.wav", "data/b.wav"]], "/repo")
        self.assertTrue(all(os.path.isabs(p) for p in sent[0]), sent)

    def test_absolute_pairs_are_unchanged(self):
        pair = ["/abs/a.wav", "/abs/b.wav"]
        self.assertEqual(pair, self._sent([pair], "/repo")[0])

    def test_no_pairs_still_returns_a_reason_not_a_silent_empty(self):
        # A metric that quietly measures nothing is the failure this whole
        # investigation keeps rediscovering.
        from experiments import library_voice_fidelity as lvf
        values, reason = lvf.ecapa_pairs([], "/usr/bin/python3")
        self.assertEqual([], values)
        self.assertIsNotNone(reason)


class GatedAdapterIdentityTests(unittest.TestCase):
    def test_unusable_explicit_gate_source_never_falls_back_to_same_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            legacy = tmp / 'legacy'
            alternate = tmp / 'alternate'
            for base in (legacy, alternate):
                (base / 'voice' / 'adapter').mkdir(parents=True)
                (base / 'voice' / 'adapter' / 'adapter_model.safetensors').write_bytes(b'unrelated weights')
            outside = tmp / 'outside'
            outside.mkdir()
            missing = legacy / 'missing' / 'adapter'
            not_directory = legacy / 'file'
            not_directory.write_bytes(b'file')
            escaped = legacy / 'linked'
            escaped.symlink_to(outside, target_is_directory=True)
            for gated in (missing, outside, not_directory, escaped):
                with self.subTest(gated=gated), \
                     patch.object(promote_adapters, 'SOURCE', str(legacy)), \
                     patch.object(promote_adapters, 'retrain_sources', return_value=(str(legacy), str(alternate))), \
                     patch.object(promote_adapters, 'gate_result', return_value={'adapter': str(gated)}):
                    self.assertIsNone(promote_adapters.get_adapter_source('voice'))
            with patch.object(promote_adapters, 'SOURCE', str(legacy)), \
                 patch.object(promote_adapters, 'retrain_sources', return_value=(str(legacy), str(alternate))), \
                 patch.object(promote_adapters, 'gate_result', return_value={}):
                self.assertEqual(str(legacy / 'voice' / 'adapter'), promote_adapters.get_adapter_source('voice'))
            with patch.object(promote_adapters, 'SOURCE', str(legacy)), \
                 patch.object(promote_adapters, 'retrain_sources', return_value=(str(legacy), str(alternate))), \
                 patch.object(promote_adapters, 'gate_result', return_value={'adapter': str(alternate / 'voice' / 'adapter')}):
                self.assertEqual(str(alternate / 'voice' / 'adapter'), promote_adapters.get_adapter_source('voice'))

    def test_unavailable_gated_weights_cannot_change_production_or_publish_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            models = tmp / 'models'
            installed = models / 'voice'
            installed.mkdir(parents=True)
            weights = installed / 'adapter_model.safetensors'
            weights.write_bytes(b'installed weights')
            manifest = models / 'manifest.json'
            manifest.write_text(json.dumps([{'id': 'voice', 'gate_ecapa': 0.4}]))
            original = manifest.read_bytes()
            legacy = tmp / 'legacy'
            alternative = legacy / 'voice' / 'adapter'
            alternative.mkdir(parents=True)
            (alternative / 'adapter_model.safetensors').write_bytes(b'unverified alternative')
            gates = tmp / 'gates'
            gates.mkdir()
            (gates / 'gate_promote__voice.json').write_text(json.dumps({
                'adapter': str(legacy / 'missing' / 'adapter'), 'median_ecapa': 0.8,
                'passed': True, 'generation_failures': 0}))
            backups = tmp / 'backups'
            with patch.object(promote_adapters, 'SOURCE', str(legacy)), \
                 patch.object(promote_adapters, 'retrain_sources', return_value=(str(legacy),)), \
                 patch.object(promote_adapters, 'GATES', str(gates)), \
                 patch.object(promote_adapters, 'GATE_PREFIX', 'gate_promote__'), \
                 patch.object(promote_adapters, 'MODELS', str(models)), \
                 patch.object(promote_adapters, 'BACKUPS', str(backups)), \
                 patch.object(promote_adapters, 'shipped_scores', return_value={'voice': 0.4}):
                self.assertEqual(1, promote_adapters.promote(['voice'], 'missing-gated', False))
            self.assertEqual(b'installed weights', weights.read_bytes())
            self.assertEqual(original, manifest.read_bytes())
            self.assertFalse(backups.exists())


class UnreadablePromotionEvidenceTests(unittest.TestCase):
    def test_evidence_failures_refuse_promotion_without_touching_installed_artifacts(self):
        import builtins, contextlib, io
        for target, unseen_mode in (('gate', False), ('baseline', False), ('manifest', False),
                                    ('unseen', True), ('baseline', True), ('manifest', True)):
            for failure in ('permission', 'directory', 'invalid_json'):
                with self.subTest(target=target, unseen_mode=unseen_mode, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    models, gates, source = root / 'models', root / 'gates', root / 'source'
                    (models / 'voice').mkdir(parents=True)
                    gates.mkdir()
                    source.mkdir()
                    weights = models / 'voice' / 'adapter_model.safetensors'
                    weights.write_bytes(b'installed voice weights')
                    (source / 'adapter_model.safetensors').write_bytes(b'candidate voice weights')
                    manifest = models / 'manifest.json'
                    manifest.write_text(json.dumps([{'id': 'voice', 'gate_ecapa': 0.4}]))
                    baseline = gates / 'library_voice_fidelity_n10.json'
                    baseline.write_text(json.dumps({'results': [{'adapter': 'voice', 'ecapa': 0.4}]}))
                    prefix = promote_adapters.UNSEEN_PREFIX if unseen_mode else 'gate_promote__'
                    gate = gates / (prefix + 'voice' + ('__clean' if unseen_mode else '') + '.json')
                    gate.write_text(json.dumps({'passed': True, 'median_ecapa': 0.8, 'adapter': str(source)}))
                    unseen = gates / 'unseen_gate__voice__shipped.json'
                    unseen.write_text(json.dumps({'median_ecapa': 0.4}))
                    paths = {'gate': gate, 'baseline': baseline, 'manifest': manifest, 'unseen': unseen}
                    damaged = paths[target]
                    if failure == 'directory':
                        damaged.unlink()
                        damaged.mkdir()
                    elif failure == 'invalid_json':
                        damaged.write_text('{broken')
                    snapshot = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    real_open = builtins.open
                    def open_evidence(path, *args, **kwargs):
                        if failure == 'permission' and Path(path) == damaged:
                            raise PermissionError('evidence access denied')
                        return real_open(path, *args, **kwargs)
                    diagnostics = io.StringIO()
                    with patch.object(promote_adapters, 'MODELS', str(models)), \
                         patch.object(promote_adapters, 'GATES', str(gates)), \
                         patch.object(promote_adapters, 'BACKUPS', str(root / 'backups')), \
                         patch.object(promote_adapters, 'GATE_PREFIX', prefix), \
                         patch.object(promote_adapters, 'get_adapter_source', return_value=str(source)), \
                         patch('builtins.open', side_effect=open_evidence), \
                         contextlib.redirect_stderr(diagnostics):
                        self.assertEqual(1, promote_adapters.promote(['voice'], 'refused', False))
                    self.assertIn('evidence', diagnostics.getvalue())
                    self.assertEqual({**snapshot, Path('models/manifest.json.lock'): b''},
                                     {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
                    self.assertFalse((root / 'backups').exists())

    def test_gate_documents_must_be_objects(self):
        for invalid in ([], 'gate', 1, None):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                gates = Path(tmp)
                (gates / 'gate_promote__voice.json').write_text(json.dumps(invalid))
                (gates / 'unseen_gate__voice__shipped.json').write_text(json.dumps(invalid))
                with patch.object(promote_adapters, 'GATES', str(gates)), \
                     patch.object(promote_adapters, 'GATE_PREFIX', 'gate_promote__'):
                    self.assertIsNone(promote_adapters.gate_result('voice'))
                    self.assertIsNone(promote_adapters.shipped_unseen_score('voice'))
                    self.assertFalse(promote_adapters.check('voice', {'voice': 0.4})[0])


class RollbackMissingMetadataTests(unittest.TestCase):
    def test_revert_removes_promoted_fields_missing_from_original_metadata(self):
        fields = ("epochs_run", "epoch_losses", "final_loss", "best_loss",
                  "sample_count", "lora_r", "lr")
        for original in ({}, {"epochs_run": 2, "lr": 0.001},
                         {"num_samples": 17}, {"sample_count": 9, "num_samples": 17}):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                models = Path(tmp, "models")
                backups = Path(tmp, "backups")
                models.mkdir()
                backup = backups / "stamp" / "voice"
                backup.mkdir(parents=True)
                metadata = backup / "training_meta.json"
                metadata.write_text(json.dumps(original))
                prior_metadata = metadata.read_bytes()
                receipt = backups / "stamp.json"
                receipt.write_text(json.dumps({"adapters": [{"adapter": "voice", "shipped_ecapa": 0.6}]}))
                prior_receipt = receipt.read_bytes()
                promoted = {"id": "voice", "description": "keep", "retrained_at": "stamp", "gate_ecapa": 0.9,
                            **{field: [9] if field == "epoch_losses" else 9 for field in fields}}
                untouched = {"id": "other", "epochs_run": 77, "retrained_at": "other-stamp"}
                manifest = models / "manifest.json"
                manifest.write_text(json.dumps([promoted, untouched]))
                with patch.object(promote_adapters, "MODELS", str(models)), \
                     patch.object(promote_adapters, "BACKUPS", str(backups)):
                    self.assertEqual(1, promote_adapters.revert_manifest({"voice"}, "stamp"))
                entries = json.loads(manifest.read_text())
                expected = {"id": "voice", "description": "keep", "gate_ecapa": 0.6}
                expected.update({field: original[field] for field in fields if field in original})
                if "num_samples" in original:
                    expected["sample_count"] = original["num_samples"]
                self.assertEqual(expected, entries[0])
                self.assertEqual(untouched, entries[1])
                self.assertEqual(prior_metadata, metadata.read_bytes())
                self.assertEqual(prior_receipt, receipt.read_bytes())
