"""Route/preference/lease repair drills (#1844 R3-R5), fail-first.

R3: rejected starts never write the preference file. Mid-inference
preference changes refuse publication. R4: the owned claim's task lease
descriptor reaches the alignment child. R5: dtype policy for old GPUs.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_readalong_qwen import _FixtureMixin, _CaptureTasks


class PreferenceAtomicityTests(_FixtureMixin):
    def test_duplicate_start_keeps_preference_bytes(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref_path = Path(tmp) / "readalong_timing.json"
        pref_path.write_text(json.dumps(
            {"timing_mode": "chunk", "timing_device": "cpu",
             "timing_language": "English"}), encoding="utf-8")
        from routers.editor import ReadaloudEpubRequest
        import core
        tasks = _CaptureTasks()
        # First start reserves the claim.
        first = editor._start_readaloud_epub(tasks, ReadaloudEpubRequest())
        self.assertEqual("started", first["status"])
        before = pref_path.read_bytes()
        # Second start is refused (duplicate claim); it must not mutate prefs.
        try:
            with self.assertRaises(HTTPException):
                editor._start_readaloud_epub(
                    tasks, ReadaloudEpubRequest(timing_language="German"))
        finally:
            core.release_gpu_task_claim("readaloud_epub",
                                        first.get("claim_id"), pending_only=True)
        self.assertEqual(before, pref_path.read_bytes())

    def test_invalid_language_keeps_preference(self):
        from fastapi import HTTPException
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        pref_path = Path(tmp) / "readalong_timing.json"
        before = pref_path.read_bytes() if pref_path.exists() else None
        from routers.editor import ReadaloudEpubRequest
        with self.assertRaises(HTTPException) as ctx:
            editor._start_readaloud_epub(
                _CaptureTasks(),
                ReadaloudEpubRequest(timing_mode="qwen", timing_language="Arabic"))
        self.assertEqual(400, ctx.exception.status_code)
        after = pref_path.read_bytes() if pref_path.exists() else None
        self.assertEqual(before, after)

    def test_chunk_mode_skips_language_gate(self):
        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        from routers.editor import ReadaloudEpubRequest
        response = editor._start_readaloud_epub(
            _CaptureTasks(),
            ReadaloudEpubRequest(timing_mode="chunk", timing_language="Arabic"))
        self.assertEqual("started", response["status"])
        self.assertEqual("chunk", response["timing_mode"])


class LeaseHandoffTests(_FixtureMixin):
    def test_claim_lease_descriptor_reaches_worker_call(self):
        """The worker task pulls the live claim lease descriptor and it
        reaches the alignment subprocess launch.

        POSIX evidence: the task-lease fd is inherited by the child
        (pass_fds). Windows native behaviour: the keeper holds the
        lease via a Job Object and the same primitive no-ops the fd;
        exclusion semantics are verified on every platform.
        """
        import os as _os
        import core
        _editor, _manager = self._fixture(self._mkdtmp())
        claim_id = core.reserve_background_task("readaloud_epub", cpu_only=False)
        try:
            from core import get_task_lease_descriptor
            fd = get_task_lease_descriptor(core.process_state["readaloud_epub"])
            self.assertIsNotNone(fd)
            self.assertIsInstance(fd, int)
            self.assertGreaterEqual(fd, 0)
            self.assertIn("readaloud_epub",
                          core.gpu_lock_conflicts("audio", cpu_only=False))
            if _os.name != "nt":
                from subprocess_ownership import get_subprocess_lease_options
                options = get_subprocess_lease_options({}, fd)
                self.assertEqual(fd, options.get("task_lease_fd"))
        finally:
            core.release_gpu_task_claim("readaloud_epub", claim_id,
                                        pending_only=True)

    def test_mid_inference_preference_change_refuses_publication(self):
        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        pref_path = Path(tmp) / "readalong_timing.json"
        pref_path.write_text(json.dumps(
            {"timing_mode": "qwen", "timing_device": "auto",
             "timing_language": None}), encoding="utf-8")
        capability = {
            "available": True, "python": sys.executable,
            "model_dir": self._model_dir(tmp), "devices": [],
            "cpu_fallback": True, "qwen_asr_version": "0.0.6",
            "torch_version": "2.10.0+cpu", "reason": None}
        from qwen_runtime import (get_qwen_provider_digest,
                                  get_qwen_runtime_identity)
        identity = get_qwen_runtime_identity(sys.executable)
        capability["runtime"] = identity
        digest = get_qwen_provider_digest(
            capability["model_dir"],
            runtime_identity={"qwen_asr_version": "0.0.6",
                              "torch_version": "2.10.0+cpu", "device": "cpu",
                              "dtype": "float32", "identity": identity})
        timing = {"timing_mode": "qwen", "device": "cpu", "dtype": "float32",
                  "language": None, "provider_digest": digest,
                  "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                  "capability": capability,
                  "runtime": identity,
                  "preference": {"timing_mode": "qwen", "timing_device": "auto",
                                 "timing_language": None},
                  "reason": "test"}
        mutator = Path(tmp) / "mutator_worker.py"
        mutator.write_text(
            "import json, sys\n"
            "job = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "open(PREF_PATH, 'w', encoding='utf-8').write(json.dumps({'timing_mode': 'chunk', 'timing_device': 'auto', 'timing_language': None}))\n"
            "import subprocess\n"
            "sys.exit(subprocess.run([sys.executable, WORKER_PATH, sys.argv[1], sys.argv[2]]).returncode)\n"
            .replace("PREF_PATH", repr(str(pref_path)))
            .replace("WORKER_PATH", repr(str(
                Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"))),
            encoding="utf-8")
        import qwen_runtime
        from readalong_export import build_epub_export
        with patch.object(qwen_runtime, "get_qwen_worker_command",
                          lambda _py: [sys.executable, str(mutator)]):
            ok, message = build_epub_export(manager, lambda: False, timing=timing)
        self.assertFalse(ok)
        self.assertIn("changed during the build", message.lower())
        self.assertFalse((Path(tmp) / "readaloud.epub").exists())

    def _model_dir(self, tmp):
        model = Path(tmp) / "model"
        model.mkdir(exist_ok=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "tokenizer_config.json").write_text("{}", encoding="utf-8")
        return str(model)


class DeviceDtypePolicyTests(unittest.TestCase):
    def test_old_card_gets_fp16(self):
        devices = [{"device": "cuda:0", "capability": [7, 5],
                    "free_bytes": 6 * 1024**3, "total_bytes": 6 * 1024**3,
                    "name": "old"}]
        device, dtype = __import__("qwen_runtime", fromlist=["x"]).get_qwen_device_selection(devices)
        self.assertEqual("cuda:0", device)
        self.assertEqual("float16", dtype)

    def test_ampre_gets_bf16_and_low_vram_ineligible(self):
        devices = [{"device": "cuda:0", "capability": [8, 6],
                    "free_bytes": 6 * 1024**3, "total_bytes": 8 * 1024**3,
                    "name": "ampere"},
                   {"device": "cuda:1", "capability": [9, 0],
                    "free_bytes": 1 * 1024**3, "total_bytes": 32 * 1024**3,
                    "name": "full"}]
        import qwen_runtime
        device, dtype = qwen_runtime.get_qwen_device_selection(devices)
        self.assertEqual("cuda:0", device)
        self.assertEqual("bfloat16", dtype)

    def test_auto_falls_back_to_cpu_when_nothing_eligible(self):
        import qwen_runtime
        device, dtype = qwen_runtime.get_qwen_device_selection(
            [{"device": "cuda:0", "capability": [8, 6],
              "free_bytes": 512 * 1024**2, "total_bytes": 4 * 1024**3}])
        self.assertEqual(("cpu", "float32"), (device, dtype))


if __name__ == "__main__":
    unittest.main()
