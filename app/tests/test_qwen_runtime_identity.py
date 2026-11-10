"""Live runtime identity (#1844 P8) — discriminating tests.

Identity must come from actually observing the selected interpreter and
its package metadata; no self-comparisons. Where real backends are
expensive, a local stub interpreter path is used BUT the capture itself
always runs the real subprocess/helper path with real metadata reads.
"""
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qwen_runtime as qr

THIS_PY = sys.executable


def _fake_site(root, version):
    site = Path(root) / "site-packages"
    info = site / ("qwen_asr-%s.dist-info" % version)
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 1.0\nName: qwen-asr\nVersion: %s\n" % version,
        encoding="utf-8")
    (site / "qwen_asr.py").write_text("# fake module\n", encoding="utf-8")


class IdentityCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qwen-p8t-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _alternate_python(self):
        root = Path(self.tmp) / "alternate-runtime"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(root)
        return str(root / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    def test_capture_reports_real_interpreter_facts(self):
        facts = qr.get_qwen_runtime_identity(THIS_PY)
        # Execute the selected venv entry; only its executable bytes resolve.
        self.assertEqual(os.path.abspath(THIS_PY), facts["python"])
        self.assertEqual(platform.python_version(), facts["python_version"])
        self.assertIn("packages", facts)
        self.assertIn("executable_sha256", facts)
        self.assertEqual(64, len(facts["executable_sha256"]))
        self.assertGreater(facts["executable_bytes"], 0)
        executable = Path(THIS_PY).resolve().read_bytes()
        self.assertEqual(hashlib.sha256(executable).hexdigest(), facts["executable_sha256"])
        self.assertEqual(len(executable), facts["executable_bytes"])

    def test_metadata_change_changes_identity(self):
        # Fake package metadata lives in an alternate venv's own
        # site-packages (never PYTHONPATH: the identity capture
        # sanitizes loader vars exactly like the worker, so a
        # PYTHONPATH shadow must not be observable).
        alternate = self._alternate_python()
        site_root = Path(alternate).parent.parent
        candidates = [site_root / "Lib" / "site-packages",
                      site_root / "lib" / "python3.10" / "site-packages",
                      site_root / "lib64" / "python3.10" / "site-packages",
                      site_root / "lib" / "site-packages"]
        site = next((c for c in candidates if c.exists()), candidates[0])
        site.mkdir(parents=True, exist_ok=True)
        info = site / "qwen_asr-0.0.1.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text(
            "Metadata-Version: 1.0\nName: qwen-asr\nVersion: 0.0.1\n",
            encoding="utf-8")
        first = qr.get_qwen_runtime_identity(alternate)
        self.assertEqual(os.path.abspath(alternate), first["python"])
        self.assertEqual(site_root.resolve(), Path(first["python_prefix"]).resolve())
        self.assertEqual("0.0.1", first["packages"].get("qwen-asr"))
        info2 = site / "qwen_asr-0.0.2.dist-info"
        shutil.rmtree(info)
        info2.mkdir(parents=True, exist_ok=True)
        (info2 / "METADATA").write_text(
            "Metadata-Version: 1.0\nName: qwen-asr\nVersion: 0.0.2\n", encoding="utf-8")
        second = qr.get_qwen_runtime_identity(alternate)
        self.assertEqual("0.0.2", second["packages"].get("qwen-asr"))
        self.assertNotEqual(first["packages"], second["packages"])
        self.assertNotEqual(first["runtime_digest"], second["runtime_digest"])

    def test_interpreter_swap_changes_identity(self):
        alternate_python = self._alternate_python()
        first = qr.get_qwen_runtime_identity(THIS_PY)
        other = qr.get_qwen_runtime_identity(alternate_python)
        self.assertNotEqual(first["runtime_digest"], other["runtime_digest"])

    def test_capture_is_stable(self):
        one = qr.get_qwen_runtime_identity(THIS_PY)
        two = qr.get_qwen_runtime_identity(THIS_PY)
        self.assertEqual(one["runtime_digest"], two["runtime_digest"])

    def test_unknown_interpreter_fails(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_runtime_identity(str(Path(self.tmp) / "missing-python.exe"))

    def test_provider_digest_binds_runtime_not_strings(self):
        model = Path(self.tmp) / "m"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="utf-8")
        from qwen_runtime import get_qwen_provider_digest
        runtime_a = qr.get_qwen_runtime_identity(THIS_PY)
        runtime_b = qr.get_qwen_runtime_identity(self._alternate_python())
        first = get_qwen_provider_digest(
            str(model), runtime_identity={
                "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                "device": "cpu", "dtype": "float32", "identity": runtime_a})
        second = get_qwen_provider_digest(
            str(model), runtime_identity={
                "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                "device": "cpu", "dtype": "float32", "identity": runtime_b})
        self.assertNotEqual(first, second)
        third = get_qwen_provider_digest(
            str(model), runtime_identity={
                "qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                "device": "cpu", "dtype": "float32", "identity": runtime_a})
        self.assertEqual(first, third)


if __name__ == "__main__":
    unittest.main()
