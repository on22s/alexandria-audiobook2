"""Real adapter artifacts, child crashes and shared writer admission; no inference."""
import contextlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import promote_adapters as promotion
from adapter_publication import CLI_PUBLICATION_JOURNAL
from fastapi import HTTPException
from routers import lora
from tests.test_support import write_test_adapter
from utils import file_lock

# An isolated saved source can reproduce the old defect without editing the tree.
if os.environ.get("PROMOTION_BASELINE_FILE"):
    spec = importlib.util.spec_from_file_location("promotion_baseline", os.environ["PROMOTION_BASELINE_FILE"])
    promotion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promotion)


def _files(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def _run_crash(operation, point):
    replace = os.replace
    def crash_after_replace(source, target):
        result = replace(source, target)
        src, dst = Path(source), Path(target)
        reached = ((point == "original" and dst.parent.name == "originals" and dst.name == "a")
                   or (point == "first" and dst == Path(promotion.MODELS, "a"))
                   or (point == "second" and dst == Path(promotion.MODELS, "b"))
                   or (point == "manifest" and dst == Path(promotion.MODELS, "manifest.json"))
                   or (point == "receipt" and dst == Path(promotion.BACKUPS, "stamp.json")))
        if point == "committed" and dst.name == CLI_PUBLICATION_JOURNAL:
            reached = json.loads(dst.read_text())["status"] == "committed"
        if reached:
            os._exit(73)
        return result
    with patch.object(promotion.os, "replace", side_effect=crash_after_replace):
        if operation == "promotion":
            promotion.promote(["a", "b"], "stamp", False)
        else:
            promotion.rollback("stamp")


class PromotionPublicationTransactionTests(unittest.TestCase):
    def fixture(self, root):
        models, source, gates, backups = (root / n for n in ("models", "source", "gates", "backups"))
        models.mkdir(); source.mkdir(); gates.mkdir()
        for name in ("a", "b"):
            write_test_adapter(models / name, value=1)
            write_test_adapter(source / name / "adapter", value=2)
            config_path = source / name / "adapter" / "adapter_config.json"
            config = json.loads(config_path.read_text()); config["lora_alpha"] = 8
            config_path.write_text(json.dumps(config))
            (models / name / "keep.txt").write_text("unowned " + name)
            (models / name / "ref_sample.wav").write_bytes(b"old reference " + name.encode())
            (source / name / "adapter" / "ref_sample.wav").write_bytes(b"new reference " + name.encode())
            (models / name / "identity_check").mkdir()
            (models / name / "identity_check" / "old.json").write_text("old identity")
            (source / name / "adapter" / "identity_check").mkdir()
            (source / name / "adapter" / "identity_check" / "new.json").write_text("new identity")
            meta = source / name / "adapter" / "training_meta.json"
            meta.write_text(json.dumps({"num_samples": 17, "best_loss": 0.5, "provenance": name}))
            (gates / ("gate_promote__" + name + ".json")).write_text(json.dumps({
                "passed": True, "median_ecapa": 0.8, "generation_failures": 0}))
        (gates / "library_voice_fidelity_n10.json").write_text(json.dumps({
            "results": [{"adapter": n, "ecapa": 0.4} for n in ("a", "b")]}))
        (models / "manifest.json").write_bytes(b'[ {"id":"a","sample_count":3}, {"id":"b","sample_count":4}, {"id":"untouched","keep":9} ]\n')
        return models, source, gates, backups

    @contextlib.contextmanager
    def configured(self, fixture):
        models, source, gates, backups = fixture
        with patch.object(promotion, "MODELS", str(models)), \
             patch.object(promotion, "SOURCE", str(source)), \
             patch.object(promotion, "GATES", str(gates)), \
             patch.object(promotion, "BACKUPS", str(backups)), \
             patch.object(promotion, "GATE_PREFIX", "gate_promote__"):
            yield

    def assert_no_journal(self, models):
        self.assertFalse((models / CLI_PUBLICATION_JOURNAL).exists())
        self.assertEqual([], list(models.glob(".promotion-*")))
        with file_lock(str(models / "manifest.json"), timeout=0):
            pass

    def test_metadata_failure_restores_every_resource_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, source, _gates, backups = fixture
            before = _files(models); original_source = _files(source)
            with self.configured(fixture), patch.object(promotion, "update_manifest", side_effect=OSError("metadata failed")):
                with self.assertRaisesRegex(OSError, "metadata failed"):
                    promotion.promote(["a", "b"], "stamp", False)
            expected = {**before, "manifest.json.lock": b""}
            self.assertEqual(expected, _files(models))
            self.assertEqual(original_source, _files(source))
            self.assertFalse((backups / "stamp.json").exists())
            for name in ("a", "b"):
                self.assertEqual(_files(models / name), _files(backups / "stamp" / name))
            self.assert_no_journal(models)

    def test_promotion_crashes_recover_exact_old_or_durably_committed_bundle(self):
        for point in ("original", "first", "second", "manifest", "receipt", "committed"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                fixture = self.fixture(Path(tmp)); models, source, _gates, backups = fixture
                before = _files(models); original_source = _files(source)
                with self.configured(fixture):
                    child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", point))
                    child.start(); child.join(10)
                    if child.is_alive():
                        child.kill(); child.join(5); self.fail("Crash fixture hung")
                    self.assertEqual(73, child.exitcode)
                    self.assertTrue((models / CLI_PUBLICATION_JOURNAL).exists())
                    # No endpoint may overwrite a pending CLI recovery, even if its
                    # old manifest or adapter path would otherwise appear valid.
                    with self.assertRaises(HTTPException) as rejected:
                        lora._promote_lora_candidate("a", str(models), str(models / "manifest.json"))
                    self.assertEqual(409, rejected.exception.status_code)
                    self.assertTrue(promotion.recover_publication())
                    self.assertFalse(promotion.recover_publication())
                if point == "committed":
                    self.assertEqual(0.8, json.loads((models / "manifest.json").read_text())[0]["gate_ecapa"])
                    self.assertEqual(2, len(json.loads((backups / "stamp.json").read_text())["adapters"]))
                    for name in ("a", "b"):
                        self.assertEqual((source / name / "adapter" / "adapter_model.safetensors").read_bytes(),
                                         (models / name / "adapter_model.safetensors").read_bytes())
                else:
                    self.assertEqual({**before, "manifest.json.lock": b""}, _files(models))
                    self.assertFalse((backups / "stamp.json").exists())
                self.assertEqual(original_source, _files(source))
                self.assert_no_journal(models)

    def test_rollback_crashes_preserve_pre_rollback_bundle_until_commit(self):
        for point in ("original", "first", "second", "manifest", "committed"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                fixture = self.fixture(Path(tmp)); models, _source, _gates, backups = fixture
                original_manifest = (models / "manifest.json").read_bytes()
                originals = {name: _files(models / name) for name in ("a", "b")}
                with self.configured(fixture):
                    self.assertEqual(0, promotion.promote(["a", "b"], "stamp", False))
                    promoted = _files(models); receipt = (backups / "stamp.json").read_bytes()
                    child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("rollback", point))
                    child.start(); child.join(10)
                    if child.is_alive():
                        child.kill(); child.join(5); self.fail("Rollback crash fixture hung")
                    self.assertEqual(73, child.exitcode)
                    self.assertTrue(promotion.recover_publication())
                    self.assertFalse(promotion.recover_publication())
                if point == "committed":
                    for name in ("a", "b"):
                        self.assertEqual(originals[name], _files(models / name))
                    manifest = json.loads((models / "manifest.json").read_text())
                    self.assertEqual(0.4, manifest[0]["gate_ecapa"])
                    self.assertNotIn("retrained_at", manifest[0])
                else:
                    self.assertEqual(promoted, _files(models))
                self.assertEqual(receipt, (backups / "stamp.json").read_bytes())
                self.assert_no_journal(models)

    def test_success_uses_installed_metadata_and_never_reuses_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, source, _gates, backups = fixture
            originals = {name: _files(models / name) for name in ("a", "b")}
            with self.configured(fixture):
                # A metadata change after directory staging must not leak into
                # the manifest independently of the installed weights.
                update = promotion.update_manifest
                def change_source(*args, **kwargs):
                    (source / "a/adapter/training_meta.json").write_text('{"num_samples":999}')
                    return update(*args, **kwargs)
                with patch.object(promotion, "update_manifest", side_effect=change_source):
                    self.assertEqual(0, promotion.promote(["a", "b"], "stamp", False))
                rows = json.loads((models / "manifest.json").read_text())
                self.assertEqual(17, rows[0]["sample_count"])
                self.assertEqual({"id":"untouched", "keep":9}, rows[2])
                for name in ("a", "b"):
                    self.assertEqual(originals[name], _files(backups / "stamp" / name))
                    self.assertEqual("unowned " + name, (models / name / "keep.txt").read_text())
                    self.assertEqual(["new.json"], [p.name for p in (models / name / "identity_check").iterdir()])
                self.assertEqual(0, promotion.rollback("stamp"))
                before = _files(models); old_receipt = (backups / "stamp.json").read_bytes()
                with self.assertRaises(FileExistsError):
                    promotion.promote(["a", "b"], "stamp", False)
                self.assertEqual(before, _files(models))
                self.assertEqual(old_receipt, (backups / "stamp.json").read_bytes())
            self.assert_no_journal(models)

    def test_cli_checks_scores_only_after_shared_manifest_lock_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, _source, _gates, _backups = fixture
            entered = multiprocessing.get_context("fork").Event()
            def run():
                scores = promotion.shipped_scores
                def observed_scores():
                    entered.set()
                    return scores()
                with patch.object(promotion, "shipped_scores", side_effect=observed_scores):
                    os._exit(promotion.promote(["a", "b"], "stamp", False))
            with self.configured(fixture):
                child = None
                try:
                    with file_lock(str(models / "manifest.json")):
                        child = multiprocessing.get_context("fork").Process(target=run)
                        child.start()
                        self.assertFalse(entered.wait(0.15))
                        self.assertTrue(child.is_alive())
                    child.join(10)
                    self.assertEqual(0, child.exitcode)
                    self.assertTrue(entered.is_set())
                finally:
                    if child is not None:
                        if child.is_alive():
                            child.kill()
                        child.join(5)
            self.assert_no_journal(models)

    def test_malformed_journal_refuses_all_http_writers_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp); (models / CLI_PUBLICATION_JOURNAL).write_text('{broken')
            (models / "manifest.json").write_text('[]')
            # The persistent lock itself is the sole permitted admission artifact.
            with file_lock(str(models / "manifest.json")):
                pass
            before = _files(models)
            for operation in (lora._promote_lora_candidate, lora._rollback_lora_promotion,
                              lora._recover_checkpoint_swap, lora._delete_rollback_backup):
                with self.subTest(operation=operation.__name__), self.assertRaises(HTTPException) as rejected:
                    operation("voice", str(models), str(models / "manifest.json"))
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(before, _files(models))
            with patch.object(promotion, "MODELS", str(models)), patch.object(promotion, "BACKUPS", str(models / "backups")):
                with self.assertRaises(ValueError):
                    promotion.recover_publication()
            self.assertEqual(before, _files(models))


    def test_interrupted_recovery_and_terminal_cleanup_are_repeatable(self):
        for point in ("adapter", "manifest", "receipt", "terminal", "cleanup"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                fixture = self.fixture(Path(tmp)); models, _source, _gates, backups = fixture
                before = _files(models)
                with self.configured(fixture):
                    child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", "receipt"))
                    child.start(); child.join(10)
                    if child.is_alive():
                        child.kill(); child.join(5); self.fail("First crash fixture hung")
                    self.assertEqual(73, child.exitcode)
                    def recover_then_crash():
                        replace, remove, rmtree = os.replace, os.remove, promotion.shutil.rmtree
                        def replaced(source, target):
                            result = replace(source, target)
                            dst = Path(target)
                            reached = ((point == "adapter" and dst == models / "a")
                                       or (point == "manifest" and dst == models / "manifest.json"))
                            if point == "terminal" and dst.name == CLI_PUBLICATION_JOURNAL:
                                reached = json.loads(dst.read_text())["status"] == "recovered"
                            if reached:
                                os._exit(74)
                            return result
                        def removed(target, *args, **kwargs):
                            result = remove(target, *args, **kwargs)
                            if point == "receipt" and Path(target) == backups / "stamp.json":
                                os._exit(74)
                            return result
                        def cleaned(target, *args, **kwargs):
                            result = rmtree(target, *args, **kwargs)
                            if point == "cleanup" and Path(target).name.startswith(".promotion-"):
                                os._exit(74)
                            return result
                        with patch.object(promotion.os, "replace", side_effect=replaced), \
                             patch.object(promotion.os, "remove", side_effect=removed), \
                             patch.object(promotion.shutil, "rmtree", side_effect=cleaned):
                            promotion.recover_publication()
                    child = multiprocessing.get_context("fork").Process(target=recover_then_crash)
                    child.start(); child.join(10)
                    if child.is_alive():
                        child.kill(); child.join(5); self.fail("Recovery crash fixture hung")
                    self.assertEqual(74, child.exitcode)
                    self.assertTrue(promotion.recover_publication())
                    self.assertFalse(promotion.recover_publication())
                self.assertEqual({**before, "manifest.json.lock": b""}, _files(models))
                self.assertFalse((backups / "stamp.json").exists())
                self.assert_no_journal(models)

    def test_corrupt_snapshot_fails_before_restoring_any_live_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, _source, _gates, _backups = fixture
            with self.configured(fixture):
                child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", "first"))
                child.start(); child.join(10)
                if child.is_alive():
                    child.kill(); child.join(5); self.fail("Corruption crash fixture hung")
                self.assertEqual(73, child.exitcode)
                journal_path = models / CLI_PUBLICATION_JOURNAL
                journal = json.loads(journal_path.read_text())
                snapshot = models / journal["workspace"] / "manifest.before"
                original = snapshot.read_bytes(); snapshot.write_bytes(b"corrupt")
                before = _files(models)
                with self.assertRaisesRegex(ValueError, "corrupt"):
                    promotion.recover_publication()
                self.assertEqual(before, _files(models))
                snapshot.write_bytes(original)
                self.assertTrue(promotion.recover_publication())
            self.assert_no_journal(models)

    def test_dry_run_does_not_recover_and_cli_refuses_pending_http_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, _source, _gates, _backups = fixture
            with self.configured(fixture):
                child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", "first"))
                child.start(); child.join(10)
                if child.is_alive():
                    child.kill(); child.join(5); self.fail("Dry-run crash fixture hung")
                self.assertEqual(73, child.exitcode)
                before = _files(models)
                with self.assertRaisesRegex(ValueError, "recovery is required"):
                    promotion.promote(["a", "b"], "next", True)
                self.assertEqual(before, _files(models))
                promotion.recover_publication()
                (models / "a" / lora.CHECKPOINT_SWAP_JOURNAL).write_text('{broken')
                before = _files(models)
                with self.assertRaisesRegex(ValueError, "checkpoint recovery"):
                    promotion.promote(["a", "b"], "next", False)
                self.assertEqual(before, _files(models))
            self.assert_no_journal(models)

    def test_partial_backup_cannot_be_accepted_for_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, _source, _gates, backups = fixture
            before = _files(models)
            copytree = promotion.shutil.copytree
            def fail_second_backup(source, target, *args, **kwargs):
                if Path(target) == backups / "stamp" / "b":
                    raise OSError("backup copy failed")
                return copytree(source, target, *args, **kwargs)
            with self.configured(fixture):
                with patch.object(promotion.shutil, "copytree", side_effect=fail_second_backup):
                    with self.assertRaisesRegex(OSError, "backup copy failed"):
                        promotion.promote(["a", "b"], "stamp", False)
                self.assertTrue((backups / "stamp" / ".incomplete").is_file())
                with self.assertRaisesRegex(ValueError, "invalid rollback"):
                    promotion.rollback("stamp")
            self.assertEqual({**before, "manifest.json.lock": b""}, _files(models))
            self.assertFalse((backups / "stamp.json").exists())
            self.assert_no_journal(models)

    def test_missing_original_cannot_be_mistaken_for_already_completed_recovery(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self.fixture(Path(tmp)); models, _source, _gates, backups = fixture
            before = _files(models)
            with self.configured(fixture):
                child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", "first"))
                child.start(); child.join(10)
                if child.is_alive():
                    child.kill(); child.join(5); self.fail("Original-loss crash fixture hung")
                self.assertEqual(73, child.exitcode)
                journal = json.loads((models / CLI_PUBLICATION_JOURNAL).read_text())
                original = models / journal["workspace"] / "originals" / "a"
                shutil.rmtree(original)
                damaged = _files(models)
                with self.assertRaisesRegex(ValueError, "corrupt or missing"):
                    promotion.recover_publication()
                self.assertEqual(damaged, _files(models))
                shutil.copytree(backups / "stamp" / "a", original)
                self.assertTrue(promotion.recover_publication())
            self.assertEqual({**before, "manifest.json.lock": b""}, _files(models))
            self.assert_no_journal(models)

    def test_fresh_cli_recover_entrypoint_restores_then_is_idempotent(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models, source, gates, _backups = self.fixture(root)
            runtime = root / "ab_test_runtime"; runtime.mkdir()
            models.rename(root / "lora_models")
            source.rename(runtime / "retrain_honest")
            gates.rename(runtime / "experiments")
            fixture = (root / "lora_models", runtime / "retrain_honest",
                       runtime / "experiments", runtime / "promotion_backups")
            models, _source, _gates, backups = fixture
            before = _files(models)
            with self.configured(fixture):
                child = multiprocessing.get_context("fork").Process(target=_run_crash, args=("promotion", "receipt"))
                child.start(); child.join(10)
                if child.is_alive():
                    child.kill(); child.join(5); self.fail("CLI restart crash fixture hung")
                self.assertEqual(73, child.exitcode)
            # Exact current executable, relocated into a normal project layout.
            # Imports use the project's installed shared helpers via PYTHONPATH.
            executable = root / "promote_adapters.py"
            executable.write_bytes(Path(promotion.__file__).read_bytes())
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            for attempt in range(2):
                result = subprocess.run([sys.executable, str(executable), "--recover"],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                if attempt == 0:
                    self.assertIn("recovered", result.stdout)
            self.assertEqual({**before, "manifest.json.lock": b""}, _files(models))
            self.assertFalse((backups / "stamp.json").exists())
            self.assert_no_journal(models)


class HttpPublicationAdmissionTests(unittest.TestCase):
    def test_actual_http_writers_refuse_pending_cli_journal_and_release_claim(self):
        import copy
        import core
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            (models / CLI_PUBLICATION_JOURNAL).write_text('{broken')
            manifest = models / "manifest.json"; manifest.write_text('[]')
            with file_lock(str(manifest)):
                pass
            before = _files(models)
            state = copy.deepcopy(core.process_state)
            for entry in state.values():
                entry["running"] = False
            engine = object()
            app = FastAPI(); app.include_router(lora.router)
            operations = (("post", "promote"), ("post", "rollback-promotion"),
                          ("post", "recover-checkpoint-swap"), ("delete", "rollback-backup"),
                          ("delete", ""))
            with patch.object(core, "process_state", state), \
                 patch.object(lora, "process_state", state), \
                 patch.object(lora, "LORA_MODELS_DIR", str(models)), \
                 patch.object(lora, "LORA_MODELS_MANIFEST", str(manifest)), \
                 patch.object(lora, "_load_builtin_lora_manifest", return_value=[]), \
                 patch.object(lora.project_manager, "engine", engine), TestClient(app) as client:
                for method, suffix in operations:
                    with self.subTest(method=method, suffix=suffix):
                        path = "/api/lora/models/voice" + ("/" + suffix if suffix else "")
                        response = getattr(client, method)(path)
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertIn("promote_adapters.py --recover", response.json()["detail"])
                        self.assertFalse(state["lora_training"]["running"])
                        self.assertIs(engine, lora.project_manager.engine)
                        self.assertEqual(before, _files(models))
