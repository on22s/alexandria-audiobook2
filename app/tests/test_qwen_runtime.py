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

    def _run_probe_with_failing_aligner(self, folder_name, platform):
        # A qwen_asr whose aligner import fails the way nagisa 0.2.11 does
        # under a non-ASCII path (QwenLM/Qwen3-ASR#214), plus a nagisa
        # package in the same site folder.
        import subprocess
        site = self.root / folder_name / "site-packages"
        (site / "qwen_asr").mkdir(parents=True)
        (site / "qwen_asr" / "__init__.py").write_text(
            "raise RuntimeError('Could not read model from nagisa_v001.model')\n",
            encoding="utf-8")
        (site / "qwen_asr-0.0.6.dist-info").mkdir()
        (site / "qwen_asr-0.0.6.dist-info" / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: qwen-asr\nVersion: 0.0.6\n", encoding="utf-8")
        (site / "nagisa").mkdir()
        (site / "nagisa" / "__init__.py").write_text("", encoding="utf-8")
        source = "import sys; sys.platform = %r\n" % platform + qr._PROBE_SOURCE
        env = dict(os.environ, PYTHONPATH=str(site), PYTHONIOENCODING="utf-8")
        completed = subprocess.run([sys.executable, "-c", source], env=env,
                                   capture_output=True, timeout=60)
        return json.loads(completed.stdout.decode("utf-8"))["reason"]

    def test_probe_names_non_ascii_install_path_on_windows(self):
        reason = self._run_probe_with_failing_aligner("José runtime", "win32")
        self.assertIn("non-ASCII", reason)
        self.assertIn("José runtime", reason)
        self.assertIn("#214", reason)

    def test_probe_keeps_plain_reason_for_ascii_path_or_other_platform(self):
        # Only the real #214 shape gets the path advice: an ASCII path on
        # Windows, or a non-ASCII path elsewhere, keeps the original error.
        for folder, platform in (("plain runtime", "win32"), ("José runtime", "linux")):
            with self.subTest(folder=folder, platform=platform):
                reason = self._run_probe_with_failing_aligner(folder, platform)
                self.assertNotIn("non-ASCII", reason)
                self.assertIn("Could not read model", reason)

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
