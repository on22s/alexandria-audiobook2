"""Phase 4 — read-only Voice Lab health summary tests.

Exercises _build_voicelab_health over the persisted run-history + checkpoint
recovery model without a live server, mirroring test_run_history.py's style.
"""

import copy
import datetime
import json
import os
import tempfile
import unittest

from routers.voicelab import _build_voicelab_health


def _iso(offset_seconds=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=offset_seconds)).isoformat()


def _write_run(history_dir, run_id, status, started_at, **extra):
    record = {"id": run_id, "task": "voicelab", "status": status,
              "started_at": started_at, "finished_at": None, "error": None,
              "artifacts": [], **extra}
    with open(os.path.join(history_dir, f"{run_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh)


def _empty_manifest(models_dir):
    path = os.path.join(models_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([], fh)
    return path


def _adapter_with_recovery(models_dir, adapter_id, operation="promote"):
    manifest_path = os.path.join(models_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump([{"id": adapter_id}], fh)
    adapter_dir = os.path.join(models_dir, adapter_id)
    os.makedirs(adapter_dir, exist_ok=True)
    with open(os.path.join(adapter_dir, ".checkpoint_swap.json"), "w", encoding="utf-8") as fh:
        json.dump({"operation": operation}, fh)
    return manifest_path


class VoiceLabHealthTests(unittest.TestCase):
    def _build(self, state=None, history_dir=None, models_dir=None, manifest_path=None):
        return _build_voicelab_health(
            state=state or {}, history_dir=history_dir, models_dir=models_dir,
            manifest_path=manifest_path, root_dir=".")

    def test_idle_when_no_history_and_no_state(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            out = self._build(history_dir=h, models_dir=m, manifest_path=_empty_manifest(m))
            self.assertEqual("idle", out["status"])
            self.assertIsNone(out["active_run"])
            self.assertIsNone(out["last_success"])
            self.assertIsNone(out["last_failure"])
            self.assertEqual([], out["pending_recovery"])
            self.assertEqual("No Voice Lab runs yet.", out["next_action"])

    def test_running_reports_stage_and_nonnegative_elapsed(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_active", "running", _iso(-30),
                       stages=[{"name": "train", "started_at": _iso(-20)}],
                       next_action="Wait for train to finish.")
            state = {"running": True, "run_id": "run_active", "current_task_idx": 0,
                     "tasks": [{"name": "train", "status": "running"}]}
            out = self._build(state=state, history_dir=h, models_dir=m,
                              manifest_path=_empty_manifest(m))
            self.assertEqual("running", out["status"])
            self.assertIsNotNone(out["active_run"])
            self.assertEqual("train", out["active_run"]["stage"])
            self.assertGreaterEqual(out["active_run"]["elapsed_seconds"], 0)
            self.assertEqual("Wait for train to finish.", out["next_action"])
            # ETA fields are always present on an active run (may be None).
            self.assertIn("eta_seconds", out["active_run"])
            self.assertIn("progress", out["active_run"])

    def test_active_run_reports_eta_from_shared_estimator(self):
        import time
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_eta", "running", _iso(-60),
                       stages=[{"name": "train", "started_at": _iso(-60)}])
            state = {"running": True, "run_id": "run_eta", "current_task_idx": 0,
                     "tasks": [{"name": "train", "status": "running"}],
                     "start_time": time.time() - 60, "logs": ["Progress: 1/4"]}
            out = self._build(state=state, history_dir=h, models_dir=m,
                              manifest_path=_empty_manifest(m))
            # 1/4 done in 60s -> a positive ETA and a progress marker.
            self.assertGreater(out["active_run"]["eta_seconds"], 0)
            self.assertIsNotNone(out["active_run"]["progress"])

    def test_newest_of_each_kind_is_selected(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_old_fail", "failed", _iso(-300), finished_at=_iso(-290))
            _write_run(h, "run_ok", "completed", _iso(-200), finished_at=_iso(-190),
                       next_action="Review generated adapters.")
            _write_run(h, "run_new_cancel", "cancelled", _iso(-100), finished_at=_iso(-90),
                       next_action="Review partial outputs.")
            out = self._build(history_dir=h, models_dir=m, manifest_path=_empty_manifest(m))
            # newest overall is cancelled -> drives status/next_action
            self.assertEqual("cancelled", out["status"])
            self.assertEqual("Review partial outputs.", out["next_action"])
            self.assertEqual("run_ok", out["last_success"]["id"])
            self.assertEqual("run_new_cancel", out["last_failure"]["id"])

    def test_failure_detail_comes_from_failing_stage(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_f", "failed", _iso(-100), finished_at=_iso(-90),
                       stages=[{"name": "dedup", "failure": None},
                               {"name": "train", "failure": {"type": "nonzero_exit",
                                                             "exit_status": 1}}])
            out = self._build(history_dir=h, models_dir=m, manifest_path=_empty_manifest(m))
            self.assertEqual("nonzero_exit", out["last_failure"]["failure"]["type"])

    def test_corrupt_history_record_is_skipped(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_ok", "completed", _iso(-50), finished_at=_iso(-40))
            with open(os.path.join(h, "run_broken.json"), "w", encoding="utf-8") as fh:
                fh.write("{ not valid json")
            out = self._build(history_dir=h, models_dir=m, manifest_path=_empty_manifest(m))
            self.assertEqual("ok", out["status"])
            self.assertEqual("run_ok", out["last_success"]["id"])

    def test_recovery_required_takes_precedence_over_success(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_ok", "completed", _iso(-50), finished_at=_iso(-40))
            manifest_path = _adapter_with_recovery(m, "narrator_alice")
            out = self._build(history_dir=h, models_dir=m, manifest_path=manifest_path)
            self.assertEqual("recovery_required", out["status"])
            self.assertEqual(1, len(out["pending_recovery"]))
            self.assertEqual("narrator_alice", out["pending_recovery"][0]["adapter_id"])
            self.assertIn("Recover", out["next_action"])

    def test_builder_does_not_mutate_state_or_write_files(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as m:
            _write_run(h, "run_active", "running", _iso(-10),
                       stages=[{"name": "quality", "started_at": _iso(-5)}])
            state = {"running": True, "run_id": "run_active", "current_task_idx": 0,
                     "tasks": [{"name": "quality", "status": "running"}]}
            before_state = copy.deepcopy(state)
            before_files = sorted(os.listdir(h))
            self._build(state=state, history_dir=h, models_dir=m,
                        manifest_path=_empty_manifest(m))
            self.assertEqual(before_state, state)
            self.assertEqual(before_files, sorted(os.listdir(h)))


if __name__ == "__main__":
    unittest.main()
