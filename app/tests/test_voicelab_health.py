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


class ProfilerPreflightIdentityTests(unittest.TestCase):
    def test_changed_profile_inputs_reject_old_preflight_before_gpu_claim(self):
        from pathlib import Path
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voicelab
        api = FastAPI()
        api.include_router(voicelab.router)
        probe = {'torch': 'fixture', 'hip': None, 'gpu': None,
                 'deps': {'llama_cpp': True}, 'python': 'fixture'}
        for changed in ('request_model', 'config_model', 'epub_dirs'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                root = Path(tmp)
                inputs = root / 'inputs'
                inputs.mkdir()
                epub = root / 'epubs'
                epub.mkdir()
                first = root / 'first.gguf'
                second = root / 'second.gguf'
                first.write_bytes(b'fixture-one')
                second.write_bytes(b'fixture-two')
                cfg = {'zips_dir': str(inputs), 'rocm_python': sys.executable,
                       'profiler_model': str(first), 'epub_dirs': []}
                body = {'stages': ['profile'], 'device': 'cpu'}
                original_cfg = copy.deepcopy(cfg)
                with patch.object(voicelab, 'DATA_DIR', tmp), \
                     patch.object(voicelab, '_load_voicelab_config', side_effect=lambda: copy.deepcopy(cfg)), \
                     patch.object(voicelab, '_probe_voicelab_interpreter', return_value=probe), \
                     patch.object(voicelab, '_run_profiler_preflight', return_value={'status': 'passed'}) as check, \
                     patch.object(voicelab.shutil, 'disk_usage', return_value=SimpleNamespace(free=30 * 1024 ** 3)), \
                     patch.object(voicelab, 'check_global_gpu_lock', side_effect=AssertionError('stale preflight reached GPU guard')) as gpu:
                    original = client.post('/api/voicelab/preflight', json=body)
                    self.assertEqual(200, original.status_code, original.text)
                    before = original.json()
                    self.assertTrue(before['ready'], before)
                    unchanged = client.post('/api/voicelab/preflight', json=body).json()
                    self.assertEqual(before['preflight_id'], unchanged['preflight_id'])
                    self.assertEqual(original_cfg, cfg)
                    if changed == 'request_model':
                        body['profiler_model'] = str(second)
                    elif changed == 'config_model':
                        cfg['profiler_model'] = str(second)
                    else:
                        cfg['epub_dirs'] = [str(epub)]
                    start = client.post('/api/voicelab/start', json={**body, 'preflight_id': before['preflight_id']})
                    self.assertEqual(409, start.status_code, start.text)
                    self.assertIn('inputs changed', start.json()['detail'])
                    gpu.assert_not_called()
                    after = client.post('/api/voicelab/preflight', json=body).json()
                    self.assertTrue(after['ready'], after)
                    self.assertNotEqual(before['preflight_id'], after['preflight_id'])
                    command = check.call_args.args[0]
                    expected_model = str(first if changed == 'epub_dirs' else second)
                    self.assertEqual(expected_model, command[command.index('--model') + 1])
                    if changed == 'epub_dirs':
                        self.assertEqual(str(epub), command[command.index('--epub-dir') + 1])
                    self.assertEqual(b'fixture-one', first.read_bytes())
                    self.assertEqual(b'fixture-two', second.read_bytes())


class VoiceLabDiskBoundaryTests(unittest.TestCase):
    def test_admission_uses_raw_bytes_while_display_rounds(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from routers import voicelab as v
        cfg = {'zips_dir': '', 'rocm_python': '', 'profiler_model': '', 'epub_dirs': []}
        cases = [(1.96, True, False), (2.0, False, True),
                 (9.96, False, True), (10.0, False, False)]
        for gib, critical, low in cases:
            with self.subTest(gib=gib), patch.object(v.shutil, 'disk_usage',
                    return_value=SimpleNamespace(free=int(gib * 1024 ** 3))):
                report = v._build_voicelab_preflight(v.VoiceLabRequest(stages=['name']), cfg)
            self.assertEqual(critical, 'disk_critical' in [r['code'] for r in report['blockers']])
            self.assertEqual(low, 'disk_low' in [r['code'] for r in report['warnings']])
            self.assertEqual(round(gib, 1), report['runtime']['free_disk_gb'])


class VoiceLabZipTypeTests(unittest.TestCase):
    def test_http_inspect_and_preflight_count_only_regular_archives(self):
        from pathlib import Path
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voicelab as v
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('narrator', '_deduped'):
                folder = root / name
                folder.mkdir()
                (folder / 'folder.zip').mkdir()
                (folder / 'real.ZIP').write_bytes(b'archive counting fixture')
                (folder / 'notes.txt').write_text('Synthetic notes')
            manifest = root / 'manifest.json'
            manifest.write_text('[]')
            cfg = {'zips_dir': tmp, 'rocm_python': '', 'profiler_model': '', 'epub_dirs': []}
            app = FastAPI()
            app.include_router(v.router)
            with patch.object(v, '_load_voicelab_config', return_value=cfg), \
                 patch.object(v, 'LORA_MODELS_MANIFEST', str(manifest)), TestClient(app) as client:
                response = client.get('/api/voicelab/inspect')
                preflight = client.post('/api/voicelab/preflight', json={'stages': ['name']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(200, preflight.status_code, preflight.text)
            self.assertEqual(1, response.json()['narrators'][0]['zip_count'])
            self.assertEqual(1, response.json()['deduped_count'])
            self.assertEqual(1, preflight.json()['dataset']['zip_count'])
            self.assertEqual(1, preflight.json()['dataset']['deduped_count'])


class VoiceLabNamingInputTests(unittest.TestCase):
    def test_name_only_needs_no_dataset_but_data_stages_still_refuse(self):
        from pathlib import Path
        from unittest.mock import patch
        from routers import voicelab as v
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'zips_dir': '', 'rocm_python': '', 'profiler_model': '', 'epub_dirs': []}
            for folder in ('', str(Path(tmp) / 'removed')):
                current = {**cfg, 'zips_dir': folder}
                with self.subTest(folder=folder), patch.object(v, 'DATA_DIR', tmp):
                    name = v._build_voicelab_preflight(v.VoiceLabRequest(stages=['name']), current)
                    self.assertTrue(name['ready'], name['blockers'])
                    for stage in ('quality', 'dedup', 'train', 'evaluate', 'profile'):
                        report = v._build_voicelab_preflight(v.VoiceLabRequest(stages=[stage]), current)
                        codes = [row['code'] for row in report['blockers']]
                        self.assertIn('zips_missing' if folder else 'zips_unconfigured', codes)


class VoiceLabMpsProbeTests(unittest.TestCase):
    def test_actual_selected_interpreter_probe_and_backend_specific_admission(self):
        from pathlib import Path
        import sys
        from unittest.mock import patch
        from routers import voicelab as v
        for backend in ('mps', 'cuda', 'cpu'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / '_deduped').mkdir()
                (root / '_deduped' / 'count.zip').write_bytes(b'counting fixture')
                interpreter = root / 'synthetic-python'
                interpreter.write_text('#!'+sys.executable+'\n'+
                    'import sys,types,importlib.util\n'+
                    'torch=types.SimpleNamespace(__version__="synthetic",version=types.SimpleNamespace(hip=None),'+
                    'cuda=types.SimpleNamespace(is_available=lambda:'+str(backend=='cuda')+','+
                    'get_device_name=lambda n:"Synthetic GPU",mem_get_info=lambda:(12*1024**3,16*1024**3)),'+
                    'backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda:'+str(backend=='mps')+')))\n'+
                    'sys.modules["torch"]=torch\nimportlib.util.find_spec=lambda name:object()\nexec(sys.argv[2])\n')
                interpreter.chmod(0o700)
                cfg = {'zips_dir': tmp, 'rocm_python': str(interpreter), 'profiler_model': '', 'epub_dirs': []}
                with patch.object(v, 'DATA_DIR', tmp):
                    probe = v._probe_voicelab_interpreter(str(interpreter))
                    for device in ('auto', 'cpu', 'mps', 'cuda'):
                        report = v._build_voicelab_preflight(v.VoiceLabRequest(stages=['train'], device=device), cfg)
                        allowed = device=='cpu' or device==backend or device=='auto' and backend!='cpu'
                        self.assertEqual(allowed, report['ready'], (backend,device,report['blockers']))
                        if backend=='mps' and device in ('auto','mps'):
                            self.assertIsNone(report['runtime']['free_vram_gb'])
                self.assertEqual(backend=='mps', probe.get('mps_available'))
                self.assertEqual(backend=='cuda', probe.get('cuda_available'))
