"""Qwen runtime discovery (#1844) — fail-first; no real inference here.

Capability probing must be bounded and never turn unknown into ready.
All interpreter/model fixtures are explicit stubs injected via the
probe_command parameter / environment; production defaults never apply
in tests.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import qwen_runtime as qr


def _write_probe(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import json, sys\n"
        "print(json.dumps(%r))\n" % (payload,), encoding="utf-8")
    return [sys.executable, str(path)]


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _complete(self):
        from tests.test_qwen_repairs import _complete_model
        return _complete_model(self.root / "tmp")

    def test_ready_with_cuda(self):
        model = self._complete()
        probe = _write_probe(self.root / "probe.py", {
            "ready": True, "torch": True, "qwen_asr": True,
            "qwen_asr_version": "0.0.6",
            "devices": [{"device": "cuda:0", "name": "fake",
                         "capability": [8, 6], "free_bytes": 6 * 1024**3,
                         "total_bytes": 8 * 1024**3}],
            "cpu_fallback": True})
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model), "ALEXANDRIA_QWEN_PYTHON": sys.executable}):
            capability = qr.get_qwen_capability(
                str(self.root), probe_command=probe)
        self.assertTrue(capability["available"])
        self.assertEqual("cuda:0", capability["device"])
        self.assertEqual("bfloat16", capability["dtype"])
        self.assertEqual("0.0.6", capability["qwen_asr_version"])
        self.assertTrue(capability["cpu_fallback"])
        self.assertEqual(str(model), capability["model_dir"])
        self.assertTrue(capability["provider_digest"])

    def test_ready_cpu_only(self):
        model = self._complete()
        probe = _write_probe(self.root / "probe.py", {
            "ready": True, "torch": True, "qwen_asr": True,
            "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True})
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model), "ALEXANDRIA_QWEN_PYTHON": sys.executable}):
            capability = qr.get_qwen_capability(str(self.root), probe_command=probe)
        self.assertTrue(capability["available"])
        self.assertEqual([], capability["devices"])

    def test_missing_imports_not_ready(self):
        model = self._complete()
        probe = _write_probe(self.root / "probe.py", {
            "ready": False, "torch": False, "qwen_asr": False,
            "qwen_asr_version": None, "devices": [], "cpu_fallback": False,
            "reason": "missing module: torch"})
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model), "ALEXANDRIA_QWEN_PYTHON": sys.executable}):
            capability = qr.get_qwen_capability(str(self.root), probe_command=probe)
        self.assertFalse(capability["available"])
        self.assertIn("torch", capability["reason"])

    def test_missing_model_not_ready(self):
        probe = _write_probe(self.root / "probe.py", {
            "ready": True, "torch": True, "qwen_asr": True,
            "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True})
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(self.root / "nope"), "ALEXANDRIA_QWEN_PYTHON": sys.executable}):
            capability = qr.get_qwen_capability(str(self.root), probe_command=probe)
        self.assertFalse(capability["available"])
        self.assertIn("model", capability["reason"].lower())

    def test_probe_error_not_ready(self):
        from tests.test_qwen_repairs import _complete_model
        model = _complete_model(self.root)
        bad = [sys.executable, "-c", "raise SystemExit(3)"]
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model), "ALEXANDRIA_QWEN_PYTHON": sys.executable}), \
                unittest.mock.patch.object(qr, "run_owned_capture", wraps=qr.run_owned_capture) as launch:
            capability = qr.get_qwen_capability(str(self.root), probe_command=bad)
        launch.assert_called_once()
        self.assertEqual(bad, launch.call_args.args[0])
        self.assertFalse(capability["available"])
        self.assertIn("exit code 3", capability["reason"])

    def test_probe_compile(self):
        # The default production probe must at least compile.
        compile(qr._PROBE_SOURCE, "<probe>", "exec")

    def test_probe_timeout_not_ready(self):
        from tests.test_qwen_repairs import _complete_model
        model = _complete_model(self.root)
        slow = [sys.executable, "-c", "import time; time.sleep(60)"]
        with unittest.mock.patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model), "ALEXANDRIA_QWEN_PYTHON": sys.executable}):
            capability = qr.get_qwen_capability(str(self.root), probe_command=slow,
                                                timeout=1)
        self.assertFalse(capability["available"])
        self.assertIn("timed out", capability["reason"].lower())


class DevicePolicyTests(unittest.TestCase):
    READY_GPU = {"available": True,
                 "devices": [{"device": "cuda:0", "name": "fake-gpu",
                              "capability": [8, 6],
                              "free_bytes": 6 * 1024**3,
                              "total_bytes": 8 * 1024**3}],
                 "cpu_fallback": True}
    READY_CPU = {"available": True, "devices": [], "cpu_fallback": True}
    NOT_READY = {"available": False, "devices": [], "cpu_fallback": False,
                 "reason": "missing module: qwen_asr"}

    def test_chunk_mode_is_cpu(self):
        policy = qr.get_qwen_device_policy("chunk", "auto", self.READY_GPU)
        self.assertEqual("chunk", policy["timing_mode"])
        self.assertEqual("cpu", policy["device"])
        self.assertTrue(policy["cpu_only"])

    def test_auto_prefers_ready_gpu_qwen(self):
        policy = qr.get_qwen_device_policy("auto", "auto", self.READY_GPU)
        self.assertEqual("qwen", policy["timing_mode"])
        self.assertEqual("cuda:0", policy["device"])
        self.assertEqual("bfloat16", policy["dtype"])
        self.assertFalse(policy["cpu_only"])

    def test_string_device_entries_are_unknown_not_ready(self):
        fake = {"available": True, "devices": ["cuda:0"], "cpu_fallback": True}
        policy = qr.get_qwen_device_policy("auto", "auto", fake)
        self.assertEqual("cpu", policy["device"])
        self.assertTrue(policy["cpu_only"])
        self.assertEqual("float32", policy["dtype"])
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "cuda:0", fake)

    def test_malformed_dict_metadata_not_ready(self):
        fake = {"available": True,
                "devices": [{"device": "cuda:0", "capability": "banana"}],
                "cpu_fallback": True}
        policy = qr.get_qwen_device_policy("auto", "auto", fake)
        self.assertEqual("cpu", policy["device"])
        self.assertTrue(policy["cpu_only"])

    def test_auto_falls_back_to_chunk_with_reason(self):
        policy = qr.get_qwen_device_policy("auto", "auto", self.NOT_READY)
        self.assertEqual("chunk", policy["timing_mode"])
        self.assertIn("not ready", policy["reason"])

    def test_auto_cpu_ready_uses_qwen_cpu(self):
        policy = qr.get_qwen_device_policy("auto", "auto", self.READY_CPU)
        self.assertEqual("qwen", policy["timing_mode"])
        self.assertEqual("cpu", policy["device"])
        self.assertTrue(policy["cpu_only"])

    def test_explicit_qwen_cpu_allowed(self):
        policy = qr.get_qwen_device_policy("qwen", "cpu", self.READY_CPU)
        self.assertEqual("qwen", policy["timing_mode"])
        self.assertTrue(policy["cpu_only"])

    def test_explicit_unavailable_qwen_refuses(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "auto", self.NOT_READY)

    def test_explicit_cuda_must_exist(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "cuda:5", self.READY_GPU)
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("qwen", "cuda:0", self.READY_CPU)

    def test_bool_and_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy("future", "auto", self.READY_GPU)
        with self.assertRaises(ValueError):
            qr.get_qwen_device_policy(True, "auto", self.READY_GPU)


class ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.model = Path(self.tmp.name) / "m"
        self.model.mkdir()
        (self.model / "config.json").write_text('{"a": 1}', encoding="utf-8")
        (self.model / "tokenizer_config.json").write_text('{"b": 2}', encoding="utf-8")

    def test_digest_stable_and_mutation_sensitive(self):
        first = qr.get_qwen_model_digest(str(self.model))
        self.assertEqual(first, qr.get_qwen_model_digest(str(self.model)))
        self.assertEqual(64, len(first))
        (self.model / "config.json").write_text('{"a": 2}', encoding="utf-8")
        self.assertNotEqual(first, qr.get_qwen_model_digest(str(self.model)))

    def test_missing_model_raises(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_model_digest(str(Path(self.tmp.name) / "absent"))


if __name__ == "__main__":
    unittest.main()
